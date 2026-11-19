"""Check deck-plan evidence. Does not score aesthetics or verify review truth."""
import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile
from review_state import freshness_issues, has_generated_units, image_integration_issues
from check_asset_plan import validate as validate_asset_plan


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def review_passed(value):
    return (isinstance(value, dict) and value.get("status") == "passed"
            and nonempty(value.get("notes")))


def design_issues(plan, root, *, require_outputs=False):
    """Check decisions before production and verify declared image outputs at delivery."""
    if not isinstance(plan, dict) or not isinstance(plan.get("delivery"), dict):
        return ["design preflight: missing deck plan or delivery settings"]
    errors = []
    if plan["delivery"].get("design_checks") != "v1":
        errors.append("delivery: set design_checks to v1 for design preflight")
    pages = plan.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(page, dict) for page in pages):
        return errors + ["design preflight: missing page roster"]
    page_ids = {page["id"] for page in pages if nonempty(page.get("id"))}
    for page in pages:
        decorative = page.get("page_role") in ("cover", "opener", "divider", "closing", "design_reference")
        units = page.get("visual_units")
        substantive_units = isinstance(units, list) and any(
            isinstance(unit, dict) and unit.get("role") in ("evidence", "explanation")
            for unit in units)
        if not decorative or substantive_units:
            for field in ("audience_question", "evidence_focus", "inference", "composition"):
                if not nonempty(page.get(field)):
                    errors.append(f"{page.get('id')}: design preflight needs {field}")

    intent = plan.get("presentation_intent")
    requires_new = intent.get("requires_new_imagery") if isinstance(intent, dict) else None
    if type(requires_new) is not bool:
        errors.append("presentation_intent: record whether the user requires new imagery")
    synthesis = plan.get("design_synthesis")
    production = synthesis.get("visual_production") if isinstance(synthesis, dict) else None
    if not isinstance(production, dict):
        return errors + ["design_synthesis: missing visual_production decision"]
    route = production.get("route")
    if not nonempty(production.get("rationale")):
        errors.append("visual_production: explain what the route achieves on the page")
    if route == "native_or_existing":
        if requires_new is True:
            errors.append("visual_production: native_or_existing cannot fulfill a request for new imagery")
        return errors
    if route not in ("new_components", "whole_page_exploration"):
        return errors + ["visual_production: missing or invalid production route"]

    targets = production.get("asset_ids")
    if not isinstance(targets, list) or not targets or not all(nonempty(aid) for aid in targets):
        return errors + ["visual_production: link production to specific asset_ids"]
    asset_plan_path = plan.get("asset_plan")
    if not nonempty(asset_plan_path):
        return errors + ["visual_production: selected route requires an asset_plan"]
    asset_file = root / asset_plan_path
    try:
        asset_plan = json.loads(asset_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return errors + ["visual_production: cannot read the asset_plan"]
    asset_errors = validate_asset_plan(asset_plan, require_v2=True)
    if asset_errors:
        return errors + [f"asset_plan: {issue}" for issue in asset_errors]
    assets = {asset["id"]: asset for asset in asset_plan["assets"]}
    if len(set(targets)) != len(targets) or not set(targets).issubset(assets):
        return errors + ["visual_production: asset_ids must be unique and exist in the asset_plan"]
    selected = [assets[aid] for aid in targets]
    for asset in selected:
        if page_ids.isdisjoint(asset["page_ids"]):
            errors.append(f"visual_production: {asset['id']} must serve a page in this deck plan")

    if route == "whole_page_exploration":
        groups = {asset.get("candidate_group") for asset in selected
                  if asset.get("candidate_group") and asset["final_role"] == "design_reference"}
        for group in groups:
            members = [asset for asset in assets.values() if asset.get("candidate_group") == group]
            if (len(members) < 2 or any(asset["final_role"] != "design_reference"
                                       or asset["id"] not in targets for asset in members)):
                errors.append(f"visual_production: include every whole-page candidate in group {group}")
        if not groups:
            errors.append("visual_production: whole_page_exploration needs a group of design-reference candidates")
    elif not any(asset["final_role"] in ("component", "page_asset") for asset in selected):
        errors.append("visual_production: new_components needs a component or page asset")

    if require_outputs:
        hashes_by_group = {}
        for asset in selected:
            output = asset_file.parent / asset["output"]
            try:
                pixels = output.read_bytes()
                if not pixels:
                    raise ValueError("Empty image output")
            except (OSError, ValueError):
                errors.append(f"visual_production: missing output for {asset['id']}")
                continue
            group = asset.get("candidate_group")
            if group:
                digest = hashlib.sha256(pixels).digest()
                hashes = hashes_by_group.setdefault(group, set())
                if digest in hashes:
                    errors.append(f"visual_production: duplicate output bytes in candidate group {group}")
                hashes.add(digest)
    return errors


def inspect_pptx(path):
    """Read actual slide order and native objects, without running Office code."""
    with ZipFile(path) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        rels = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in rels
                   if rel.get("TargetMode") != "External"}
        slides = []
        for sid in presentation.findall("p:sldIdLst/p:sldId", NS):
            target = targets[sid.get(f"{{{NS['r']}}}id")]
            part = (target.lstrip("/") if target.startswith("/") else
                    posixpath.normpath(posixpath.join("ppt", target)))
            slide = ET.fromstring(archive.read(part))
            image_hashes = []
            rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            if rel_part in archive.namelist():
                slide_rels = ET.fromstring(archive.read(rel_part))
                used_ids = {blip.get(f"{{{NS['r']}}}embed") for blip in slide.findall(".//a:blip", NS)}
                for rel in slide_rels:
                    if rel.get("Id") in used_ids and rel.get("TargetMode") != "External":
                        media = rel.get("Target", "")
                        media_part = (media.lstrip("/") if media.startswith("/") else
                                      posixpath.normpath(posixpath.join(posixpath.dirname(part), media)))
                        image_hashes.append(hashlib.sha256(archive.read(media_part)).hexdigest())
            # Excludes speaker notes, alt text and image metadata. Includes groups.
            # Joining runs with spaces would break words split by formatting.
            paragraphs = ["".join(t.text or "" for t in p.findall(".//a:t", NS))
                          for p in slide.findall(".//a:p", NS)]
            text = "\n".join(paragraphs)
            diagram_shapes = sum(
                sp.find("p:nvSpPr/p:cNvSpPr", NS) is not None
                and sp.find("p:nvSpPr/p:cNvSpPr", NS).get("txBox") not in ("1", "true")
                for sp in slide.findall(".//p:sp", NS)
            ) + len(slide.findall(".//p:cxnSp", NS))
            slides.append({"text": text,
                           "shapes": len(slide.findall(".//p:sp", NS)),
                           "diagram_shapes": diagram_shapes,
                           "pictures": len(slide.findall(".//p:pic", NS)),
                           "image_sha256": image_hashes,
                           "tables": len(slide.findall(".//a:tbl", NS)),
                           "charts": len(slide.findall(".//c:chart", NS))})
        return slides


def check(plan_path, require_fresh=False):
    root = plan_path.parent
    errors = []
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ["Cannot read a valid deck plan"]
    if not isinstance(plan, dict):
        return ["Deck plan must be an object"]
    if plan.get("schema_version") != 2:
        errors.append("Requires schema_version 2; migrate and review legacy plans")
    if require_fresh or (isinstance(plan.get("delivery"), dict) and "design_checks" in plan["delivery"]):
        errors.extend(design_issues(plan, root, require_outputs=True))
    if not isinstance(plan.get("project_analysis"), str) or not plan["project_analysis"].strip():
        errors.append("Missing project analysis")
    synthesis = plan.get("design_synthesis")
    if not isinstance(synthesis, dict):
        errors.append("Missing project-derived design_synthesis")
    else:
        for field in ("story", "audience_priorities", "visual_language", "delivery_rationale"):
            if not nonempty(synthesis.get(field)):
                errors.append(f"design_synthesis: missing {field}")
        needs = synthesis.get("visual_needs")
        if not isinstance(needs, list) or not needs or not all(nonempty(n) for n in needs):
            errors.append("design_synthesis: visual_needs must name project-specific communication needs")
    pages = plan.get("pages")
    if not isinstance(pages, list) or not pages:
        return errors + ["Missing page roster"]

    # A generated image must have a separately reviewable semantic brief.  The
    # deck plan may point to it with a project-relative asset_plan path.
    asset_plan_path = plan.get("asset_plan")
    generated_declared = any(
        isinstance(page, dict)
        and any(isinstance(unit, dict) and unit.get("production") == "generated"
                for unit in (page.get("visual_units") or []))
        for page in pages
    )
    if generated_declared and not isinstance(asset_plan_path, str):
        errors.append("generated visual units require an asset_plan path and semantic asset contract")
    asset_roles = {}
    design_hashes = set()
    if isinstance(asset_plan_path, str):
        asset_plan_file = root / asset_plan_path
        if not asset_plan_file.is_file():
            errors.append(f"asset_plan: file absent or empty: {asset_plan_path}")
        else:
            try:
                asset_plan = json.loads(asset_plan_file.read_text(encoding="utf-8-sig"))
                errors.extend(f"asset_plan: {issue}" for issue in validate_asset_plan(asset_plan, require_v2=require_fresh))
                if isinstance(asset_plan, dict):
                    for asset in asset_plan.get("assets", []):
                        if isinstance(asset, dict) and isinstance(asset.get("output"), str):
                            asset_path = (asset_plan_file.parent / asset["output"]).resolve()
                            asset_roles[str(asset_path)] = asset
                            if (asset.get("asset_role") == "design_reference" or
                                    asset.get("final_role") == "design_reference") and asset_path.is_file():
                                design_hashes.add(hashlib.sha256(asset_path.read_bytes()).hexdigest())
            except (OSError, ValueError):
                errors.append("asset_plan: cannot read a valid JSON asset plan")

    # Track placements so one generated still cannot silently stand in for
    # several explanatory pages.  Intentional reuse must be explicit on every
    # referencing page and carry a reason that can be reviewed.
    asset_placements = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        for asset in page.get("asset_paths") or []:
            asset_placements.setdefault(asset, []).append(page)
    for asset, placements in asset_placements.items():
        if len(placements) < 2:
            continue
        undeclared = [page.get("id", "unknown") for page in placements
                      if not (isinstance(page.get("asset_reuse"), dict)
                              and page["asset_reuse"].get("allowed") is True
                              and nonempty(page["asset_reuse"].get("reason")))]
        if undeclared:
            ids = ", ".join(str(page.get("id", "unknown")) for page in placements)
            errors.append(f"asset {asset} is placed on multiple pages ({ids}); declare asset_reuse.allowed with a reason or create page-specific assets")
    seen = set()

    def file_path(value, label):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: missing file path")
            return None
        path = root / value
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"{label}: file absent or empty: {value}")
            return None
        return path

    delivery = plan.get("delivery")
    slides = None
    if not isinstance(delivery, dict):
        errors.append("Missing delivery mode and exported artifact")
        delivery = {}
    else:
        if delivery.get("stage") not in ("sample", "final"):
            errors.append("delivery: stage must be sample or final")
        if delivery.get("format") not in ("pptx", "pdf", "png", "web"):
            errors.append("delivery: unsupported or missing format")
        artifact = file_path(delivery.get("artifact"), "delivery artifact")
        if not review_passed(delivery.get("review")):
            errors.append("delivery: exported artifact review not passed with observations")
        if delivery.get("format") == "pptx":
            mode = delivery.get("pptx_mode")
            if mode not in ("native", "raster-preview"):
                errors.append("delivery: explicitly declare native or raster-preview PPTX")
            if mode == "raster-preview":
                if not nonempty(delivery.get("raster_reason")):
                    errors.append("delivery: raster-preview requires a disclosed reason")
                if delivery.get("stage") == "final" and delivery.get("raster_authorized") is not True:
                    errors.append("delivery: final raster PPTX requires explicit user authorization")
            if artifact:
                try:
                    slides = inspect_pptx(artifact)
                    if len(slides) != len(pages):
                        errors.append("delivery: actual PPTX slide count differs from page plan")
                except (OSError, BadZipFile, KeyError, ET.ParseError, AttributeError, ValueError):
                    errors.append("delivery: cannot inspect valid PPTX slide objects")

    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            errors.append(f"Page {number}: must be an object")
            continue
        pid = page.get("id")
        if not isinstance(pid, str) or not pid or pid in seen:
            errors.append(f"Page {number}: missing or duplicate id")
            pid = f"page-{number}"
        seen.add(pid)
        for field in ("takeaway", "layout_intent", "visual_reason"):
            if not isinstance(page.get(field), str) or not page[field].strip():
                errors.append(f"{pid}: missing {field}")
        role = page.get("page_role")
        if not nonempty(role):
            errors.append(f"{pid}: missing page_role")
        units = page.get("visual_units")
        if not isinstance(units, list) or not units:
            errors.append(f"{pid}: missing project-grounded visual_units")
            units = []
        has_explanation = False
        for index, unit in enumerate(units, 1):
            label = f"{pid} visual unit {index}"
            if not isinstance(unit, dict):
                errors.append(f"{label}: must be an object")
                continue
            unit_role = unit.get("role")
            if unit_role not in ("evidence", "explanation", "decoration"):
                errors.append(f"{label}: invalid role")
            if unit.get("production") not in ("data", "native", "generated", "supplied"):
                errors.append(f"{label}: invalid production method")
            if unit_role == "evidence" and unit.get("production") == "generated":
                errors.append(f"{label}: generated imagery cannot be factual evidence")
            for field in ("purpose", "project_anchor"):
                if not nonempty(unit.get(field)):
                    errors.append(f"{label}: missing {field}")
            if unit_role in ("evidence", "explanation"):
                has_explanation = True
                for field in ("relationship", "claim_boundary"):
                    if not nonempty(unit.get(field)):
                        errors.append(f"{label}: missing {field}")
                paths = unit.get("source_paths")
                if not isinstance(paths, list) or not paths:
                    errors.append(f"{label}: no source grounding")
                else:
                    for value in paths:
                        file_path(value, label)
        if role not in ("cover", "opener", "divider", "closing") and not has_explanation:
            errors.append(f"{pid}: content page has decoration only; needs explanation/evidence")

        visual_mode = page.get("visual_mode")
        if role not in ("cover", "opener", "divider", "closing"):
            if visual_mode not in ("generated", "native_diagram", "data_chart", "mixed"):
                errors.append(f"{pid}: content page needs visual_mode; text_only pages cannot pass")
            elif visual_mode == "generated" and not page.get("image_required"):
                errors.append(f"{pid}: visual_mode generated requires image_required=true")
        is_mechanism = role == "mechanism" or "mechanism_visual" in page
        if is_mechanism:
            mechanism_visual = page.get("mechanism_visual")
            if not isinstance(mechanism_visual, dict):
                errors.append(f"{pid}: mechanism page needs mechanism_visual with visible states and relationships")
            else:
                for field in ("input_state", "operation", "output_state", "visible_relationship"):
                    if not nonempty(mechanism_visual.get(field)):
                        errors.append(f"{pid}: mechanism_visual missing {field}")
                if mechanism_visual.get("implementation") not in ("generated", "native", "mixed", "supplied"):
                    errors.append(f"{pid}: mechanism_visual implementation must identify the visual layer")
                elif mechanism_visual.get("implementation") == "generated":
                    errors.append(f"{pid}: mechanism page cannot rely on generated imagery alone; rebuild exact relationships natively")
            if not any(isinstance(unit, dict) and unit.get("role") in ("explanation", "evidence")
                       and unit.get("production") in ("native", "data", "supplied") for unit in units):
                errors.append(f"{pid}: mechanism page needs a native/data/supplied explanatory unit; generated-only units cannot establish exact relationships")
        if not review_passed(page.get("semantic_review")):
            errors.append(f"{pid}: semantic review not passed with observations")

        if delivery.get("format") == "pptx" and delivery.get("pptx_mode") == "native":
            anchors = page.get("editable_text")
            if not isinstance(anchors, list) or not anchors or not all(nonempty(a) for a in anchors):
                errors.append(f"{pid}: editable_text must list main native text, not just page numbers")
                anchors = []
            actual = slides[number - 1] if slides is not None and number <= len(slides) else None
            if actual is not None:
                normalize = lambda text: "".join(text.split())
                if not actual["text"].strip():
                    errors.append(f"{pid}: native PPTX has no native text; image-only slide rejected")
                for anchor in anchors:
                    if normalize(anchor) not in normalize(actual["text"]):
                        errors.append(f"{pid}: main editable text absent from PPTX: {anchor}")
            objects = page.get("native_objects", {})
            if not isinstance(objects, dict):
                errors.append(f"{pid}: native_objects must be an object")
            else:
                for kind, count in objects.items():
                    if kind not in ("charts", "tables", "shapes", "diagram_shapes") or type(count) is not int or count < 0:
                        errors.append(f"{pid}: invalid native object requirement")
                    elif actual is not None and actual[kind] < count:
                        errors.append(f"{pid}: expected {count} native {kind}, found {actual[kind]}")
                if is_mechanism and not any(type(count) is int and count > 0 for count in objects.values()):
                    errors.append(f"{pid}: mechanism page needs at least one declared native reconstruction object")
                if is_mechanism and require_fresh and not any(
                        type(objects.get(kind)) is int and objects[kind] > 0
                        for kind in ("diagram_shapes", "charts", "tables")):
                    errors.append(f"{pid}: mechanism reconstruction must declare diagram_shapes, charts or tables; title text boxes alone do not qualify")
        if slides is not None and number <= len(slides):
            actual = slides[number - 1]
            if (delivery.get("stage") == "final" or role not in ("design_reference", "sample")):
                if design_hashes.intersection(actual["image_sha256"]):
                    errors.append(f"{pid}: PPTX embeds a design_reference image; renaming or omitting its asset path does not reconstruct the page")
        arrays = {}
        for field in ("evidence", "authoring_files", "asset_paths", "asset_refs"):
            value = page.get(field)
            if not isinstance(value, list):
                errors.append(f"{pid}: {field} must be a list")
                value = []
            arrays[field] = value
        if not arrays["authoring_files"]:
            errors.append(f"{pid}: no authoring files")
        source = ""
        for value in arrays["authoring_files"]:
            path = file_path(value, pid)
            if path:
                try:
                    source += path.read_text(encoding="utf-8-sig") + "\n"
                except (UnicodeError, OSError):
                    errors.append(f"{pid}: cannot read authoring source")
        for value in arrays["evidence"]:
            file_path(value, f"{pid} source evidence")
        assets, refs = arrays["asset_paths"], arrays["asset_refs"]
        known_generated_asset = any(isinstance(value, str)
                                    and str((root / value).resolve()) in asset_roles
                                    for value in assets)
        if "image_use" in page or (require_fresh and
                                  (has_generated_units(page) or known_generated_asset)):
            errors.extend(f"{pid}: {issue}" for issue in image_integration_issues(page))
        if not isinstance(page.get("image_required"), bool):
            errors.append(f"{pid}: image_required must be boolean")
        if page.get("image_required") and not assets:
            errors.append(f"{pid}: required image missing from plan")
        if len(assets) != len(refs):
            errors.append(f"{pid}: asset paths/references must correspond")
        for value in assets:
            path = file_path(value, f"{pid} asset")
            if path:
                declared = asset_roles.get(str(path.resolve()))
                if declared and declared.get("asset_role") == "design_reference":
                    if delivery.get("stage") == "final" or role not in ("design_reference", "sample"):
                        errors.append(f"{pid}: design_reference asset cannot be placed as a final page asset; reconstruct it natively")
                if declared and declared.get("final_role") == "design_reference":
                    if delivery.get("stage") == "final" or role not in ("design_reference", "sample"):
                        errors.append(f"{pid}: asset final_role=design_reference is restricted to review/sample pages")
        for ref in refs:
            if not isinstance(ref, str) or not ref or ref not in source:
                errors.append(f"{pid}: asset reference missing from authoring source")
        file_path(page.get("preview"), f"{pid} final preview")
        review = page.get("visual_review")
        if not review_passed(review):
            errors.append(f"{pid}: visual review not recorded as passed with observations")
        motion = page.get("motion")
        if not isinstance(motion, dict) or not isinstance(motion.get("requested"), bool):
            errors.append(f"{pid}: missing motion decision")
        elif motion["requested"] and (motion.get("implemented") is not True or not str(motion.get("evidence", "")).strip()):
            errors.append(f"{pid}: requested motion has no implementation evidence")
    if require_fresh and delivery.get("review_tracking") != "sha256-v1":
        errors.append("delivery: sha256-v1 review tracking is required for this run")
    errors.extend(freshness_issues(plan, root))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--require-fresh-reviews", action="store_true",
                        help="Require version-bound reviews for new or revised decks")
    parser.add_argument("--design-preflight", action="store_true",
                        help="Check the argument and visual production plan before creating slides or assets")
    args = parser.parse_args()
    if args.design_preflight:
        try:
            plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
            issues = design_issues(plan, args.plan.resolve().parent)
        except (OSError, ValueError):
            issues = ["Cannot read a valid deck plan"]
        print(f"{'FAIL' if issues else 'PASS'} (design plan structure): {len(issues)} issue(s)")
        for issue in issues:
            print(issue)
        return int(bool(issues))
    issues = check(args.plan.resolve(), require_fresh=args.require_fresh_reviews)
    report = args.plan.resolve().with_name("deck-check.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"passed": not issues, "issues": issues,
        "scope": "Contract, source, recorded reviews and PPTX objects only; relevance, visibility, scientific accuracy and playback require inspection"}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'FAIL' if issues else 'PASS'} (structural checks only): {len(issues)} issue(s); {report}")
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
