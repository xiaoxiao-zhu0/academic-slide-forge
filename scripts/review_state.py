"""Bind manually inspected reviews to exact plan and file versions, not quality scores."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

TRACKING = "sha256-v1"


def clean_plan(plan):
    value = copy.deepcopy(plan)
    value.get("delivery", {}).pop("review", None)
    for page in value.get("pages", []):
        if isinstance(page, dict):
            page.pop("semantic_review", None)
            page.pop("visual_review", None)
    return value


def snapshot(plan, root, page_id=None):
    value = clean_plan(plan)
    pages = value.pop("pages")
    if page_id is not None:
        pages = [page for page in pages if page.get("id") == page_id]
        if len(pages) != 1:
            raise ValueError("Review page ID must uniquely identify a page")
    value["pages"] = pages
    paths = [value["delivery"]["artifact"]]
    if page_id is None and value["delivery"].get("design_checks") == "v1" and value.get("asset_plan"):
        asset_plan_path = Path(value["asset_plan"])
        assets = json.loads((root / asset_plan_path).read_text(encoding="utf-8-sig"))
        production = value["design_synthesis"]["visual_production"]
        paths.append(str(asset_plan_path))
        paths.extend(str(asset_plan_path.parent / asset["output"])
                     for asset in assets["assets"] if asset["id"] in production.get("asset_ids", []))
    for page in pages:
        for field in ("evidence", "authoring_files", "asset_paths"):
            paths.extend(page[field])
        paths.append(page["preview"])
        for unit in page["visual_units"]:
            paths.extend(unit.get("source_paths", []))
    hashes = {}
    for name in sorted(set(paths)):
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Review requires nonempty source, asset, preview and delivery files")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {"plan": value, "files": hashes}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def freshness_issues(plan, root):
    delivery = plan.get("delivery", {})
    if not isinstance(delivery, dict):
        return []  # Structural checker reports malformed delivery.
    tracking = delivery.get("review_tracking")
    if tracking is None:
        return []  # Legacy v2 compatibility, no freshness guarantee.
    if tracking != TRACKING:
        return ["delivery: unsupported review_tracking"]
    errors = []
    try:
        for page in plan["pages"]:
            expected = snapshot(plan, root, page["id"])
            for kind in ("semantic_review", "visual_review"):
                review = page.get(kind)
                if not isinstance(review, dict) or review.get("snapshot") != expected:
                    errors.append(f"{page['id']}: {kind} missing or stale; inspect current files again")
        review = delivery.get("review")
        if not isinstance(review, dict) or review.get("snapshot") != snapshot(plan, root):
            errors.append("delivery: review missing or stale; inspect current export again")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        errors.append("Cannot verify review freshness: invalid plan or missing review inputs")
    return errors


def has_generated_units(page):
    return any(isinstance(unit, dict) and unit.get("production") == "generated"
               for unit in (page.get("visual_units") or []))


def image_integration_issues(page, review=None):
    """Check declared intent and inspection evidence, never image quality itself."""
    errors = []
    use = page.get("image_use")
    if not isinstance(use, dict):
        errors.append("image_use: record generation intent, route and expected contribution")
        use = {}
    else:
        if use.get("intent") not in ("explanation", "visual_design", "atmosphere"):
            errors.append("image_use: intent must be explanation, visual_design or atmosphere")
        if use.get("route") not in ("whole_page_concept", "component_composition", "single_asset"):
            errors.append("image_use: route must identify the chosen production approach")
        for field in ("route_reason", "expected_contribution"):
            if not isinstance(use.get(field), str) or not use[field].strip():
                errors.append(f"image_use: missing {field}")
    if review is None:
        review = page.get("visual_review")
    integration = review.get("image_integration") if isinstance(review, dict) else None
    if not isinstance(integration, dict):
        errors.append("image_integration: inspect the composed page, not only the asset")
        return errors
    if integration.get("status") != "passed":
        errors.append("image_integration: composed-page assessment not passed")
    fields = ["visible_contribution", "removal_test", "intent_match"]
    explanatory = use.get("intent") == "explanation" or any(
        isinstance(unit, dict) and unit.get("production") == "generated"
        and unit.get("role") == "explanation" for unit in (page.get("visual_units") or []))
    if explanatory:
        fields.append("native_correspondence")
    for field in fields:
        if not isinstance(integration.get(field), str) or not integration[field].strip():
            errors.append(f"image_integration: missing {field}")
    return errors


def record_review(plan, root, kind, notes, page_id=None, image_integration=None):
    if not notes.strip():
        raise ValueError("Concrete inspection observations are required")
    if plan["delivery"].get("review_tracking") != TRACKING:
        raise ValueError("Set delivery.review_tracking to sha256-v1 before reviewing")
    if kind not in ("semantic", "visual", "delivery"):
        raise ValueError("Unsupported review kind")
    if (kind == "delivery") != (page_id is None):
        raise ValueError("Page reviews need --page; delivery reviews must omit it")
    if image_integration is not None and kind != "visual":
        raise ValueError("Image integration observations belong to a visual page review")
    current = snapshot(plan, root, page_id)
    review = {"status": "passed", "notes": notes.strip(), "snapshot": current}
    if image_integration is not None:
        review["image_integration"] = copy.deepcopy(image_integration)
    if kind == "delivery":
        plan["delivery"]["review"] = review
    else:
        page = next(page for page in plan["pages"] if page["id"] == page_id)
        if kind == "visual" and (has_generated_units(page) or "image_use" in page
                                 or image_integration is not None):
            issues = image_integration_issues(page, review)
            if issues:
                raise ValueError("; ".join(issues))
        page[kind + "_review"] = review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--kind", choices=("semantic", "visual", "delivery"), required=True)
    parser.add_argument("--page")
    parser.add_argument("--notes", required=True, help="Actual inspection observations; not automatic approval")
    parser.add_argument("--image-integration", type=Path,
                        help="JSON object of fresh composed-page observations; visual reviews only")
    args = parser.parse_args()
    path = args.plan.resolve()
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"))
        integration = (json.loads(args.image_integration.read_text(encoding="utf-8-sig"))
                       if args.image_integration else None)
        record_review(plan, path.parent, args.kind, args.notes, args.page, integration)
        temp = path.with_name(path.name + ".review.tmp")
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.error(str(exc))
    print("Recorded review version. This does not independently certify quality.")


if __name__ == "__main__":
    main()
