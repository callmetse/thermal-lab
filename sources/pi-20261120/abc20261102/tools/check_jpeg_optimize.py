"""Check OpenCV JPEG Huffman optimization on real replay frames, without saving images."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent.parent


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resize_max_side(image, side):
    height, width = image.shape[:2]
    if max(height, width) <= side:
        return image
    scale = side / max(height, width)
    return cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                      interpolation=cv2.INTER_AREA)


def blank_stats():
    return {"frames": 0, "raw_bytes": 0, "optimized_bytes": 0,
            "pixel_different_frames": 0, "shape_different_frames": 0,
            "max_abs_pixel_difference": 0, "different_source_frame_ids_first_20": [],
            "raw_encode_ms": [], "optimized_encode_ms": [],
            "raw_decode_ms": [], "optimized_decode_ms": []}


def compare_encodings(image, quality, source_frame_id, stats):
    decoded = []
    for optimize, prefix in [(0, "raw"), (1, "optimized")]:
        start = time.perf_counter_ns()
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality,
                                                 cv2.IMWRITE_JPEG_OPTIMIZE, optimize])
        stats[prefix + "_encode_ms"].append((time.perf_counter_ns() - start) / 1e6)
        if not ok:
            raise RuntimeError("JPEG encoding failed at source frame {} ({})".format(source_frame_id, prefix))
        stats[prefix + "_bytes"] += int(encoded.nbytes)
        start = time.perf_counter_ns()
        restored = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        stats[prefix + "_decode_ms"].append((time.perf_counter_ns() - start) / 1e6)
        if restored is None:
            raise RuntimeError("JPEG decoding failed at source frame {} ({})".format(source_frame_id, prefix))
        decoded.append(restored)
    stats["frames"] += 1
    equal = np.array_equal(decoded[0], decoded[1])
    if not equal:
        stats["pixel_different_frames"] += 1
        if len(stats["different_source_frame_ids_first_20"]) < 20:
            stats["different_source_frame_ids_first_20"].append(source_frame_id)
        if decoded[0].shape != decoded[1].shape:
            stats["shape_different_frames"] += 1
        else:
            difference = int(np.max(np.abs(decoded[0].astype(np.int16) - decoded[1].astype(np.int16))))
            stats["max_abs_pixel_difference"] = max(stats["max_abs_pixel_difference"], difference)
    return equal, decoded[0]


def finish_stats(stats):
    result = {key: value for key, value in stats.items() if not isinstance(value, list) or not key.endswith("_ms")}
    for key in ["raw_encode_ms", "optimized_encode_ms", "raw_decode_ms", "optimized_decode_ms"]:
        result[key.replace("_ms", "_p50_ms")] = percentile(stats[key], .5)
        result[key.replace("_ms", "_p95_ms")] = percentile(stats[key], .95)
    result["raw_mean_bytes"] = stats["raw_bytes"] / stats["frames"] if stats["frames"] else None
    result["optimized_mean_bytes"] = stats["optimized_bytes"] / stats["frames"] if stats["frames"] else None
    result["byte_reduction_fraction"] = 1 - stats["optimized_bytes"] / stats["raw_bytes"] if stats["raw_bytes"] else None
    result["all_decoded_pixels_equal"] = bool(stats["frames"] and not stats["pixel_different_frames"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/replay.mp4")
    parser.add_argument("--output", type=Path, default=ROOT / "results/jpeg-optimize-check.json")
    parser.add_argument("--source-max-side", type=int, default=640)
    parser.add_argument("--source-quality", type=int, default=55)
    parser.add_argument("--preview-max-side", type=int, default=480)
    parser.add_argument("--preview-quality", type=int, default=35)
    parser.add_argument("--max-frames", type=int, default=0, help="0 means all frames")
    args = parser.parse_args()
    if min(args.source_max_side, args.preview_max_side) < 1 or args.max_frames < 0:
        parser.error("Image sides must be positive and max-frames must be nonnegative")
    if not 0 <= args.source_quality <= 100 or not 0 <= args.preview_quality <= 100:
        parser.error("JPEG quality must be between 0 and 100")
    if args.input.resolve() == args.output.resolve():
        parser.error("Input video and JSON output must differ")
    if not hasattr(cv2, "IMWRITE_JPEG_OPTIMIZE"):
        raise RuntimeError("Installed OpenCV lacks IMWRITE_JPEG_OPTIMIZE")
    source_hash = sha256(args.input)
    cap = cv2.VideoCapture(str(args.input))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("Cannot open replay: " + str(args.input))
    fps = cap.get(cv2.CAP_PROP_FPS)
    declared_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    source, preview = blank_stats(), blank_stats()
    source_sizes, preview_sizes = set(), set()
    frames = skipped_preview = 0
    original_size = None
    start = time.perf_counter()
    try:
        while not args.max_frames or frames < args.max_frames:
            ok, image = cap.read()
            if not ok:
                break
            if original_size is None:
                original_size = [image.shape[1], image.shape[0]]
            resized = resize_max_side(image, args.source_max_side)
            source_sizes.add((resized.shape[1], resized.shape[0]))
            equal, source_decoded = compare_encodings(resized, args.source_quality, frames, source)
            if equal:
                small = resize_max_side(source_decoded, args.preview_max_side)
                preview_sizes.add((small.shape[1], small.shape[0]))
                compare_encodings(small, args.preview_quality, frames, preview)
            else:
                # Do not assume an interchangeable source decoder when pixels differ.
                skipped_preview += 1
            frames += 1
    finally:
        cap.release()
    if not frames:
        raise RuntimeError("Replay contained no decodable frames")
    source_result, preview_result = finish_stats(source), finish_stats(preview)
    result = {
        "scope": "Real replay encode/decode comparison only; no model weights, training, image exports or end-to-end network measurements",
        "input": str(args.input), "source_sha256": source_hash, "opencv": cv2.__version__,
        "numpy": np.__version__, "frames_read": frames, "original_size": original_size,
        "source_fps": fps if math.isfinite(fps) and fps > 0 else None,
        "declared_source_frames": int(declared_frames) if math.isfinite(declared_frames) and declared_frames >= 0 else None,
        "max_frames": args.max_frames, "wall_seconds": time.perf_counter() - start,
        "profile": {"source_max_side": args.source_max_side, "source_jpeg_quality": args.source_quality,
                    "preview_max_side": args.preview_max_side, "preview_jpeg_quality": args.preview_quality,
                    "resize": "INTER_AREA, round dimensions, no upscaling",
                    "comparison": "IMWRITE_JPEG_OPTIMIZE=0 vs 1; quality unchanged; decode IMREAD_COLOR",
                    "timing": "One timed imencode/imdecode per variant per frame; encode order 0 then 1; resize and comparison excluded from component times"},
        "source_sizes": [list(size) for size in sorted(source_sizes)],
        "preview_sizes": [list(size) for size in sorted(preview_sizes)],
        "source": source_result, "preview": preview_result,
        "preview_frames_skipped_due_to_source_pixel_difference": skipped_preview,
        "all_model_input_pixels_equal": source_result["all_decoded_pixels_equal"],
        "all_preview_pixels_equal": preview_result["all_decoded_pixels_equal"] and skipped_preview == 0,
        "runtime_changed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
