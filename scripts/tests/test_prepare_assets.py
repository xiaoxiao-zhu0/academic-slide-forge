"""Runner behavior without relay credentials or paid API requests."""
import base64
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_assets as runner


class AssetRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {
            "ASSET_IMAGE_ENDPOINT": "http://127.0.0.1:1/images/generations",
            "ASSET_IMAGE_MODEL": "test-image", "ASSET_IMAGE_SIZE": "1536x1024",
            "ASSET_IMAGE_QUALITY": "", "ASSET_IMAGE_API_KEY": "local-test-only"})
        env.start()
        self.addCleanup(env.stop)

    def jobs(self, count=1):
        plan = {"assets": [{
            "id": f"A{i}", "prompt": f"Scene {i} fixture", "output": f"images/{i}.png",
            "analysis_basis": "fixture input", "visual_brief": "fixture decoration",
            "prompt_review": "fixture prompt reviewed", "asset_role": "decoration",
            "generation_mode": "prompt-only", "page_ids": ["P01"],
            "project_terms": ["fixture", "scene"]
        } for i in range(count)]}
        jobs = runner.prepare_jobs(self.root / "plan.json", plan)
        runner.configure_jobs(jobs)
        return jobs

    def cache(self, job):
        output = job["output"]
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "red").save(output)
        runner.atomic_json(output.with_suffix(".png.json"), {
            "request_signature": job["signature"], "sha256": runner.digest(output)})

    def strict_jobs(self, count=2):
        jobs = self.jobs(count)
        for i, job in enumerate(jobs):
            job.update(exploration_mode="exploratory", candidate_group="concepts",
                       candidate_count_required=2, composition_id=f"layout-{i}",
                       user_review_required=True, reference_use="research_only",
                       reference_influence_policy="isolated_prompt_only",
                       final_role="design_reference", native_reconstruction_required=True)
        return jobs

    def strict_cache(self, job):
        self.cache(job)
        sidecar = job["output"].with_suffix(".png.json")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta.update(runner.contract_metadata(job))
        runner.atomic_json(sidecar, meta)
        return sidecar, meta

    def test_strict_cache_requires_all_provenance_even_null_values(self):
        job = self.strict_jobs()[0]
        sidecar, metadata = self.strict_cache(job)
        self.assertTrue(runner.reusable(job))
        for field in runner.contract_metadata(job):
            with self.subTest(field=field):
                changed = dict(metadata)
                del changed[field]
                runner.atomic_json(sidecar, changed)
                self.assertFalse(runner.reusable(job))
        runner.atomic_json(sidecar, metadata)
        job["user_review_required"] = False
        self.assertFalse(runner.reusable(job))
        job["user_review_required"] = True
        job["candidate_group"] = None
        self.assertFalse(runner.reusable(job))

    def test_current_contract_checked_before_config_or_requests(self):
        with self.assertRaisesRegex(ValueError, "contract="):
            runner.prepare_jobs(self.root / "plan.json", {"assets": []}, require_v2=True)

    def test_exploration_state_distinguishes_complete_partial_and_failed(self):
        jobs = self.strict_jobs()
        for job in jobs:
            self.strict_cache(job)
        state_path = self.root / "state.json"
        self.assertEqual(0, runner.run_jobs(jobs, state_path, self.root / "unused.env"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("awaiting-review", state["stage"])
        self.assertEqual(2, state["candidate_groups"]["concepts"]["successful_count"])
        self.assertEqual(0, runner.run_jobs(jobs[:1], state_path, self.root / "unused.env"))
        self.assertEqual("partial", json.loads(state_path.read_text(encoding="utf-8"))["stage"])
        with patch.object(runner, "generate_job", return_value={"status": "failed"}):
            self.assertEqual(1, runner.run_jobs(jobs, state_path, self.root / "unused.env", force=True))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("failed", state["stage"])
        self.assertEqual(0, state["candidate_groups"]["concepts"]["successful_count"])

    def test_single_reviewable_asset_is_not_auto_accepted(self):
        job = self.strict_jobs()[0]
        job.update(exploration_mode="single", candidate_count_required=1, candidate_group=None)
        self.strict_cache(job)
        state_path = self.root / "state.json"
        runner.run_jobs([job], state_path, self.root / "unused.env")
        self.assertEqual("awaiting-review", json.loads(state_path.read_text(encoding="utf-8"))["stage"])

    def test_uncertain_asset_survives_subset_force_and_explicit_retry(self):
        jobs = self.strict_jobs()
        state_path = self.root / "plan.state.json"
        def respond(job, *_):
            if job["id"] == "A1":
                return {"status": "unknown", "error": "remote completion unknown"}
            self.strict_cache(job)
            return {"status": "generated"}
        with patch.object(runner, "generate_job", side_effect=respond):
            runner.run_jobs(jobs, state_path, self.root, workers=1)
        with patch.object(runner, "generate_job") as generate:
            runner.run_jobs(jobs, state_path, self.root, selected=["A0"])
            runner.run_jobs(jobs, state_path, self.root, selected=["A1"], force=True)
            generate.assert_not_called()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("unknown", state["assets"]["A1"]["status"])
        self.assertEqual(["A0"], state["candidate_groups"]["concepts"]["successful_candidates"])
        def finish(job, *_):
            self.strict_cache(job)
            return {"status": "generated"}
        with patch.object(runner, "generate_job", side_effect=finish) as generate:
            runner.run_jobs(jobs, state_path, self.root, selected=["A1"], retry_unknown=["A1"])
            self.assertEqual([jobs[1]], [call.args[0] for call in generate.call_args_list])
        self.assertEqual("awaiting-review", json.loads(state_path.read_text(encoding="utf-8"))["stage"])

    def test_status_is_local_and_read_only_for_legacy_state(self):
        jobs = self.jobs(2)
        plan = self.root / "plan.json"
        assets = [{**job, "output": str(job["output"])} for job in jobs]
        runner.atomic_json(plan, {"assets": assets})
        state_path = plan.with_suffix(".state.json")
        runner.atomic_json(state_path, {"assets": {"A1": {"status": "failed",
                           "error": "Image HTTP transport failed (curl 28); provider output withheld"}}})
        before = state_path.read_bytes()
        with patch.object(sys, "argv", ["runner", "--plan", str(plan), "--status"]), \
                patch.object(runner, "load_env_file") as env, patch.object(runner, "configure_jobs") as config, \
                patch.object(runner, "generate_job") as generate, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, runner.main())
        for action in (env, config, generate):
            action.assert_not_called()
        state = json.loads(output.getvalue())
        self.assertEqual("pending", state["assets"]["A0"]["status"])
        self.assertEqual("unknown", state["assets"]["A1"]["status"])
        self.assertEqual(before, state_path.read_bytes())

    def test_interrupted_batch_is_not_resubmitted(self):
        jobs = self.jobs()
        state_path = self.root / "plan.state.json"
        runner.atomic_json(state_path, {"assets": {"A0": {"status": "queued"}}})
        with patch.object(runner, "generate_job") as generate:
            self.assertEqual(1, runner.run_jobs(jobs, state_path, self.root))
            generate.assert_not_called()

    def test_child_transport_deadlines_remain_uncertain(self):
        job = self.jobs()[0]
        for error in (b"Image HTTP transport failed (curl 28); remote completion unknown",
                      b"Image request timed out; remote completion unknown", b"Image proxy HTTP 504"):
            with self.subTest(error=error), patch.object(runner.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 1, stderr=error)):
                self.assertEqual("unknown", runner.generate_job(job, self.root, 1)["status"])

    def test_paths_resolve_from_plan_and_duplicates_reject(self):
        self.assertEqual(self.root / "images/0.png", self.jobs()[0]["output"])
        duplicate = {"assets": [{
            "id": str(i), "prompt": "fixture scene", "output": "same.png",
            "analysis_basis": "fixture input", "visual_brief": "fixture decoration",
            "prompt_review": "fixture prompt reviewed", "asset_role": "decoration",
            "generation_mode": "prompt-only", "page_ids": ["P01"],
            "project_terms": ["fixture", "scene"]
        } for i in range(2)]}
        with self.assertRaises(ValueError):
            runner.prepare_jobs(self.root / "plan.json", duplicate)
        duplicate["assets"][1].update(id="0", output="different.png")
        with self.assertRaises(ValueError):
            runner.prepare_jobs(self.root / "plan.json", duplicate)

    def test_cache_tracks_prompt_model_size_quality_and_endpoint(self):
        for field, value in (("prompt", "new scene"), ("model", "new-model"),
                             ("size", "1024x1024"), ("quality", "high")):
            with self.subTest(field=field):
                job = self.jobs()[0]
                self.cache(job)
                self.assertTrue(runner.reusable(job))
                job[field] = value
                runner.configure_jobs([job])
                self.assertFalse(runner.reusable(job))
        job = self.jobs()[0]
        self.cache(job)
        with patch.dict(os.environ, {"ASSET_IMAGE_ENDPOINT": "http://127.0.0.1:2/images/generations"}):
            runner.configure_jobs([job])
        self.assertFalse(runner.reusable(job))

    def test_corrupt_or_legacy_cache_rejects(self):
        job = self.jobs()[0]
        self.cache(job)
        job["output"].write_bytes(b"not an image")
        self.assertFalse(runner.reusable(job))
        # Even matching hashes cannot make corrupt image bytes valid.
        runner.atomic_json(job["output"].with_suffix(".png.json"), {
            "request_signature": job["signature"], "sha256": runner.digest(job["output"])})
        self.assertFalse(runner.reusable(job))
        self.cache(job)
        runner.atomic_json(job["output"].with_suffix(".png.json"), {"sha256": runner.digest(job["output"])})
        self.assertFalse(runner.reusable(job))

    def test_auto_model_discovered_once_per_batch(self):
        jobs = self.jobs(3)
        for job in jobs:
            job["model"] = "auto"
        with patch.object(runner, "discover_image_model", return_value="resolved-image") as discovery:
            runner.configure_jobs(jobs)
        discovery.assert_called_once()
        self.assertEqual({"resolved-image"}, {job["model"] for job in jobs})

    def test_bounded_parallelism_and_partial_progress_survive_failure(self):
        jobs = self.jobs(4)
        state_path = self.root / "state.json"
        lock, both_started = threading.Lock(), threading.Event()
        active = peak = 0

        def generate(job, env, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
            if not both_started.wait(2):
                raise RuntimeError("not concurrent")
            time.sleep(0.02)
            with lock:
                active -= 1
            if job["id"] == "A1":
                return {"status": "failed", "error": "fixture failure"}
            self.cache(job)
            return {"status": "generated"}

        snapshots = []
        original_write = runner.atomic_json
        def observe(path, value):
            original_write(path, value)
            if path == state_path:
                snapshots.append(json.loads(path.read_text(encoding="utf-8")))

        with patch.object(runner, "generate_job", side_effect=generate), patch.object(runner, "atomic_json", side_effect=observe):
            self.assertEqual(1, runner.run_jobs(jobs, state_path, self.root / "unused.env", workers=2))
        self.assertEqual(2, peak)
        self.assertEqual(6, len(snapshots))  # Initial, each completion, final.
        self.assertEqual("failed", snapshots[-1]["stage"])
        self.assertTrue(runner.reusable(jobs[0]))
        with patch.object(runner, "generate_job", return_value={"status": "generated"}) as generate:
            self.assertEqual(0, runner.run_jobs(jobs, state_path, self.root / "unused.env"))
        self.assertEqual(1, generate.call_count)  # Only failed asset retried by this explicit run.

    def test_timeout_or_invalid_result_preserves_previous_image(self):
        job = self.jobs()[0]
        self.cache(job)
        before = job["output"].read_bytes()
        with patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            result = runner.generate_job(job, self.root / "unused.env", 1)
        self.assertIn("remote completion unknown", result["error"])
        self.assertEqual(before, job["output"].read_bytes())
        with patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            result = runner.generate_job(job, self.root / "unused.env", 1)
        self.assertEqual("failed", result["status"])
        self.assertEqual(before, job["output"].read_bytes())

    def test_real_child_process_against_local_fake_relay(self):
        payloads = []
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                response = json.dumps({"data": [{"b64_json": encoded}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"ASSET_IMAGE_ENDPOINT": f"http://127.0.0.1:{server.server_port}/images/generations"}):
                job = self.strict_jobs()[0]
                result = runner.generate_job(job, self.root / "unused.env", 20)
                self.assertEqual("generated", result["status"])
                self.assertTrue(runner.reusable(job))
                self.assertEqual(job["prompt"], payloads[0]["prompt"])
                self.assertEqual(job["model"], payloads[0]["model"])
                self.assertFalse(any(key in payloads[0] for key in ("image", "images", "reference_images")))
                sidecar = json.loads(job["output"].with_suffix(".png.json").read_text(encoding="utf-8"))
                for key, value in runner.contract_metadata(job).items():
                    self.assertIn(key, sidecar)
                    self.assertEqual(value, sidecar[key])
                self.assertEqual([], sidecar["reference_sha256"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
