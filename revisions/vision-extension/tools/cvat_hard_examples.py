"""Prepare CVAT for images 1.1 tasks and join reviewed boxes to the original samples."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from PIL import Image

CLASS_IDS = {"person": 0}
SPLITS = {"train", "validation", "test"}


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def checked_box(box, width, height):
    if (not isinstance(box, list) or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
        raise ValueError("Expected four finite pixel coordinates")
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("Box lies outside the image or has no area")
    return box


def make_package(outbox, destination, arm=None, teacher=None):
    """Verify byte identities before preparing images and optional teacher proposals."""
    if destination.exists():
        raise FileExistsError(destination)
    summary = load(outbox / "dataset_manifest.json")
    rows = [json.loads(line) for line in (outbox / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    if summary.get("corrupted") != [] or summary.get("samples") != len(rows):
        raise ValueError("Export has missing or corrupted samples")
    samples = {}
    for row in rows:
        if arm and row.get("arm") != arm:
            continue
        sample = row["sample_id"]
        if not re.fullmatch("[a-f0-9]{16}", sample) or sample in samples:
            raise ValueError("Invalid or duplicate sample ID")
        name = sample + ".jpg"
        image_path = outbox / "samples" / name
        digest = sha256(image_path)
        if (row.get("verified") is not True or digest != row["media_sha256"]
                or digest != row["verified_sha256"] or not digest.startswith(sample)):
            raise ValueError(f"Unverified image: {sample}")
        with Image.open(image_path) as image:
            if image.format != "JPEG":
                raise ValueError(f"Expected JPEG: {sample}")
            width, height = image.size
            image.verify()
        if row.get("encoded_size") not in (None, [width, height]):
            raise ValueError(f"Recorded image size mismatch: {sample}")
        samples[sample] = {"image": name, "width": width, "height": height, "metadata": row}
    if not samples:
        raise ValueError("No samples match the selected arm")

    annotations = ET.Element("annotations")
    ET.SubElement(annotations, "version").text = "1.1"
    meta = ET.SubElement(annotations, "meta")
    task = ET.SubElement(meta, "task")
    ET.SubElement(task, "mode").text = "annotation"
    labels = ET.SubElement(task, "labels")
    label = ET.SubElement(labels, "label")
    ET.SubElement(label, "name").text = "person"
    ET.SubElement(label, "attributes")
    teacher_samples = teacher["samples"] if teacher is not None else {}
    if teacher is not None and (not teacher["model"] or not re.fullmatch("[a-f0-9]{64}", teacher["sha256"])
                               or not set(teacher_samples) <= samples.keys()):
        raise ValueError("Teacher must identify its model and selected samples")
    for image_id, (sample, entry) in enumerate(samples.items()):
        width, height = entry["width"], entry["height"]
        image = ET.SubElement(annotations, "image", id=str(image_id), name=entry["image"],
                              width=str(width), height=str(height))
        proposal = teacher_samples.get(sample)
        if proposal is None:
            continue
        if proposal["media_sha256"] != entry["metadata"]["media_sha256"]:
            raise ValueError(f"Teacher proposal refers to a different image: {sample}")
        for box in proposal["boxes"]:
            coords = checked_box(box["xyxy"], width, height)
            confidence = box["confidence"]
            if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
                raise ValueError("Invalid teacher confidence")
            ET.SubElement(image, "box", label="person", source="auto", occluded="0", z_order="0",
                          **dict(zip(("xtl", "ytl", "xbr", "ybr"), map(str, coords))))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as tmp:
        staging = Path(tmp) / "package"
        (staging / "images").mkdir(parents=True)
        for entry in samples.values():
            copied = staging / "images" / entry["image"]
            shutil.copyfile(outbox / "samples" / entry["image"], copied)
            if sha256(copied) != entry["metadata"]["media_sha256"]:
                raise ValueError(f"Source image changed during copy: {entry['image']}")
        dump(staging / "sample_map.json", {"schema_version": "cvat_hard_examples_v1", "class_ids": CLASS_IDS,
                                          "samples": samples, "teacher": teacher})
        if teacher is not None:
            ET.ElementTree(annotations).write(staging / "teacher.xml", encoding="utf-8", xml_declaration=True)
        dump(staging / "review_template.json", {
            "task_id": None, "reviewer": None, "annotation_revision": None,
            "samples": {
                key: {"decision": None, "split": None, "near_duplicate_group": None,
                      "capture_group": entry["metadata"].get("capture_group"),
                      "source_clip_sha256": entry["metadata"].get("source_sha256")}
                for key, entry in samples.items()}})
        staging.rename(destination)
    return {"samples": len(samples), "bytes": sum(row["metadata"]["media_bytes"] for row in samples.values()),
            "gate_states": dict(Counter(row["metadata"].get("gate_state") or "unknown" for row in samples.values())),
            "with_teacher_proposals": len(teacher_samples)}


def join_reviews(package, xml_path, review_path):
    mapping_path = package / "sample_map.json"
    mapping = load(mapping_path)
    review = load(review_path)
    samples = mapping["samples"]
    if (not samples or mapping["class_ids"] != CLASS_IDS
            or set(samples) != set(review["samples"])
            or any(not isinstance(review.get(key), str) or not review[key].strip()
                   for key in ("task_id", "reviewer", "annotation_revision"))):
        raise ValueError("Review must name the task, reviewer and revision and cover every sample")
    root = ET.parse(xml_path).getroot()
    if root.tag != "annotations" or root.findtext("version") != "1.1" or root.findall("track"):
        raise ValueError("Expected a CVAT for images 1.1 annotation export")
    task_id = root.findtext("meta/task/id")
    if task_id != review["task_id"]:
        raise ValueError("Review task ID and CVAT export do not match")
    by_image = {entry["image"]: key for key, entry in samples.items()}
    exported = {}
    cvat_ids = set()
    for image in root.findall("image"):
        name = image.get("name")
        image_id = image.get("id")
        if name not in by_image or name in exported or image_id is None or image_id in cvat_ids:
            raise ValueError(f"Unexpected or duplicate CVAT image: {name}")
        cvat_ids.add(image_id)
        entry = samples[by_image[name]]
        width, height = entry["width"], entry["height"]
        if (int(image.get("width")), int(image.get("height"))) != (width, height):
            raise ValueError(f"CVAT image size changed: {name}")
        boxes = []
        for box in image:
            if box.tag != "box" or box.get("label") not in CLASS_IDS or float(box.get("rotation", "0")):
                raise ValueError("Expected axis-aligned person boxes")
            coords = checked_box([float(box.get(k)) for k in ("xtl", "ytl", "xbr", "ybr")], width, height)
            x1, y1, x2, y2 = coords
            boxes.append({"class_id": CLASS_IDS[box.get("label")], "xyxy_px": coords,
                          "xyxy_normalized": [x1 / width, y1 / height, x2 / width, y2 / height],
                          "occluded": box.get("occluded") == "1"})
        exported[name] = (image_id, boxes)

    records = []
    split_by_group = {}
    for sample, entry in samples.items():
        decision = review["samples"][sample]
        state = decision.get("decision")
        if state not in ("positive", "confirmed_empty", "ambiguous"):
            raise ValueError(f"Missing explicit review decision: {sample}")
        meta = entry["metadata"]
        if sha256(package / "images" / entry["image"]) != meta["media_sha256"]:
            raise ValueError(f"Image hash changed: {sample}")
        image_id, boxes = exported.get(entry["image"], (None, []))
        if (state == "positive" and not boxes) or (state == "confirmed_empty" and boxes):
            raise ValueError(f"Review decision disagrees with CVAT boxes: {sample}")
        split = decision.get("split")
        if state == "ambiguous":
            if split is not None:
                raise ValueError("Ambiguous images cannot enter a dataset split")
        else:
            capture_group = decision.get("capture_group")
            if split not in SPLITS or not isinstance(capture_group, str) or not capture_group.strip():
                raise ValueError(f"Set a split and capture group for {sample}")
            groups = {"capture": capture_group, "run": meta.get("run_id"),
                      "recorded_capture": meta.get("capture_group"),
                      "clip": decision.get("source_clip_sha256"),
                      "near_duplicate": decision.get("near_duplicate_group")}
            if meta.get("source") == "replay" and not re.fullmatch("[a-f0-9]{64}", groups["clip"] or ""):
                raise ValueError(f"Replay sample needs its original clip SHA-256: {sample}")
            if meta.get("source_sha256") and groups["clip"] != meta["source_sha256"]:
                raise ValueError(f"Review clip SHA-256 disagrees with capture: {sample}")
            for kind, group in groups.items():
                if group and split_by_group.setdefault((kind, group), split) != split:
                    raise ValueError(f"{kind} group crosses splits: {group}")
        records.append({"sample_id": sample, "media_sha256": meta["media_sha256"], "image": entry["image"],
                        "cvat_image_id": image_id, "metadata": meta, "assessment": decision,
                        "boxes": boxes})
    return {"schema_version": "reviewed_hard_examples_v1", "class_ids": CLASS_IDS,
            "task_id": task_id, "reviewer": review["reviewer"], "annotation_revision": review["annotation_revision"],
            "input_hashes": {"cvat_xml": sha256(xml_path), "review": sha256(review_path), "sample_map": sha256(mapping_path)},
            "split_counts": dict(Counter(row["assessment"]["split"] or "excluded" for row in records)),
            "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    pack = subs.add_parser("pack")
    pack.add_argument("outbox", type=Path)
    pack.add_argument("destination", type=Path)
    pack.add_argument("--arm", choices=("A", "B", "C", "D"))
    pack.add_argument("--teacher", type=Path)
    join = subs.add_parser("join")
    for arg in ("package", "xml", "review", "destination"):
        join.add_argument(arg, type=Path)
    args = parser.parse_args()
    if args.action == "pack":
        result = make_package(args.outbox, args.destination, args.arm, load(args.teacher) if args.teacher else None)
    else:
        manifest = join_reviews(args.package, args.xml, args.review)
        with args.destination.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
        result = {"records": len(manifest["records"]), "split_counts": manifest["split_counts"]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
