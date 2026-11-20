"""Report control-clock metrics. Never invent a clock-offset correction."""
import argparse
import json
from pathlib import Path
import statistics


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    position = (len(v) - 1) * p
    lo = int(position)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (position - lo)


def summarize(rows):
    runs = {}
    for row in rows:
        p = row["payload"]
        rid = p.get("run_id")
        if not rid:
            continue
        key = (rid, row.get("console_boot_id", "unknown"))
        r = runs.setdefault(key, {"arm": p.get("arm"), "observations": [], "llm": [], "acks": [], "events": []})
        channel = row["channel"]
        if channel == "commands" and p.get("op") == "start":
            r.setdefault("started", row["received_mono_ms"])
        if channel == "commands" and p.get("op") == "stop":
            r.setdefault("stopped", row["received_mono_ms"])
        if channel in ("observations", "llm"):
            if channel == 'observations' and row.get('duplicate', False):
                r['duplicate_observations'] = r.get('duplicate_observations', 0) + 1
                continue
            r[channel].append(row)
        elif channel == "display_ack":
            r["acks"].append(row)
        elif channel == "events":
            r["events"].append(row)
    output = []
    for (rid, boot), r in runs.items():
        all_observations = r["observations"]
        deadline = r.get("stopped", float("inf"))
        obs = [x for x in all_observations if x["received_mono_ms"] <= deadline]
        all_llm = r["llm"]
        r["observations"] = obs
        r["llm"] = [x for x in all_llm if x["received_mono_ms"] <= deadline]
        spans = [x["received_mono_ms"] for x in obs]
        infer = [x["payload"]["infer_ms"] for x in obs if isinstance(x["payload"].get("infer_ms"), (float, int))]
        tracks = [t for x in obs for t in x["payload"].get("tracks", [])]
        first = lambda channel: ((min(x["received_mono_ms"] for x in r[channel]) - r["started"]) if r[channel] and "started" in r else None)
        transport = [x["payload"] for x in r["events"] if x["payload"].get("kind") == "transport_stats"]
        output.append({"run_id": rid, "console_boot_id": boot, "arm": r["arm"], "observations": len(obs),
                       "duplicate_observations": r.get('duplicate_observations', 0),
                       "late_observations_after_stop": len(all_observations) - len(obs),
                       "late_llm_after_stop": len(all_llm) - len(r["llm"]),
                       "llm_generated": sum(x["payload"].get("status") == "generated" for x in r["llm"]),
                       "llm_results": len(r["llm"]),
                       "observed_delivery_hz": ((len(obs) - 1) * 1000 / (max(spans) - min(spans))) if len(spans) > 1 and max(spans) > min(spans) else None,
                       "infer_p50_ms": percentile(infer, .5), "infer_p95_ms": percentile(infer, .95),
                       "start_to_first_observation_ms": first("observations"),
                       "start_to_first_llm_ms": first("llm"),
                       "max_observation_gap_ms": max((b - a for a, b in zip(sorted(spans), sorted(spans)[1:])), default=None),
                       "unknown_action_fraction": sum(t["action"].get("value") in (None, "unknown") for t in tracks) / len(tracks) if tracks else None,
                       "clo_met_numerical_coverage": sum(t.get("clo") is not None and t.get("met") is not None for t in tracks) / len(tracks) if tracks else None,
                       "console_to_ack_p95_ms": percentile([a["payload"]["console_to_ack_ms"] for a in r["acks"]], .95),
                       "transport": transport, "physical_accuracy": "unmeasured",
                       "capture_to_display_ms": None, "cross_machine_clock": "not_validated"})
    return output


def main():
    p = argparse.ArgumentParser()
    p.add_argument("jsonl", type=Path)
    p.add_argument("--output", type=Path, default=Path("results/summary.json"))
    a = p.parse_args()
    rows = [json.loads(line) for line in a.jsonl.read_text().splitlines() if line.strip()]
    result = summarize(rows)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
