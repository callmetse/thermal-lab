"""Stream archived console records to full JSONL and optional per-person CSV."""
import argparse
import csv
import json
from pathlib import Path
import sys
from contextlib import ExitStack


ROOT = Path(__file__).resolve().parent.parent
CSV_FIELDS = [
    "project", "run_id", "producer_instance_id", "observation_seq", "producer_recorded_wall_ms",
    "arm", "source", "console_boot_id", "received_wall_ms",
    "received_mono_ms", "source_frame_id", "source_pts_s", "producer", "status",
    "infer_ms", "row_type", "track_count", "track_id", "bbox_json", "posture_state",
    "posture_reason", "action_value", "action_reason", "clothing_json", "clothing_age_s",
    "clo", "met", "estimate_status", "window_json", "visible_clo_reference_json",
]


def json_cell(value):
    return "" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def person_rows(record):
    if record.get("channel") != "observations":
        return
    p = record["payload"]
    tracks = p.get("tracks") or []
    common = {k: p.get(k) for k in ["project", "run_id", "producer_instance_id", "observation_seq", "arm", "source", "source_frame_id",
                                  "source_pts_s", "producer", "status", "infer_ms"]}
    common.update({k: record.get(k) for k in ["producer_recorded_wall_ms", "console_boot_id", "received_wall_ms", "received_mono_ms"]})
    common["track_count"] = len(tracks)
    if not tracks:
        yield {**common, "row_type": "empty_observation"}
        return
    for track in tracks:
        posture, action = track.get("posture") or {}, track.get("action") or {}
        yield {**common, "row_type": "track", "track_id": track.get("track_id"),
               "bbox_json": json_cell(track.get("bbox")),
               "posture_state": posture.get("state"), "posture_reason": posture.get("reason"),
               "action_value": action.get("value"), "action_reason": action.get("reason"),
               "clothing_json": json_cell(track.get("clothing")), "clothing_age_s": track.get("clothing_age_s"),
               "clo": track.get("clo"), "met": track.get("met"),
               "estimate_status": track.get("estimate_status"),
               "window_json": json_cell(track.get("window")),
               "visible_clo_reference_json": json_cell(track.get("visible_clo_reference"))}


def export_records(source, output, run_ids=None, channels=None, people_csv=None):
    run_ids, channels = set(run_ids or []), set(channels or [])
    writer = csv.DictWriter(people_csv, fieldnames=CSV_FIELDS) if people_csv is not None else None
    if writer:
        writer.writeheader()
    counts = {"scanned_records": 0, "exported_records": 0, "csv_rows": 0}
    for line_number, line in enumerate(source, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise ValueError("Expected a console record with an object payload")
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid event at line {}: {}".format(line_number, error)) from error
        counts["scanned_records"] += 1
        if run_ids and record["payload"].get("run_id") not in run_ids:
            continue
        if channels and record.get("channel") not in channels:
            continue
        # Keep the full original record, including timestamps and unknown fields.
        output.write(line if line.endswith("\n") else line + "\n")
        counts["exported_records"] += 1
        if writer:
            for row in person_rows(record):
                writer.writerow(row)
                counts["csv_rows"] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/events.jsonl")
    parser.add_argument("--run-id", action="append", default=[], help="Repeat to select multiple runs")
    parser.add_argument("--channel", action="append", default=[], help="Repeat: observations, llm, events, commands, preview, display_ack")
    parser.add_argument("--output", type=Path, help="Full JSONL destination; stdout when omitted")
    parser.add_argument("--people-csv", type=Path, help="Optional flattened observations, including empty observations")
    args = parser.parse_args(argv)
    targets = [p.resolve() for p in [args.output, args.people_csv] if p is not None]
    if args.input.resolve() in targets or len(targets) != len(set(targets)):
        parser.error("Input, JSONL output and CSV output must have different paths")
    try:
        with ExitStack() as stack:
            source = stack.enter_context(args.input.open("r", encoding="utf-8"))
            for target in [args.output, args.people_csv]:
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
            output = stack.enter_context(args.output.open("w", encoding="utf-8", newline="")) if args.output else sys.stdout
            people = stack.enter_context(args.people_csv.open("w", encoding="utf-8-sig", newline="")) if args.people_csv else None
            counts = export_records(source, output, args.run_id, args.channel, people)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(counts, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
