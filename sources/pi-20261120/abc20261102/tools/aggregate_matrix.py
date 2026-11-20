"""Reproduce per-arm summaries without counting warmup or post-stop results."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from report import summarize


def aggregate(events, matrix, output_prefix):
    trials = [r for r in matrix if r['repeat'] > 0]
    ids = {r['run_id'] for r in trials}
    rows = [r for r in events if r['payload'].get('run_id') in ids]
    reports = summarize(rows)
    if len(reports) != len(trials):
        raise ValueError('Missing trials or multiple console clocks: inspect events before pooling')
    result = {'method': 'Mean of per-run metrics; inference quantiles are averaged, not pooled. '
                        'Delivery Hz is within the observed stream, excluding startup and EOF idle time.',
              'accuracy': 'unmeasured_no_human_or_physical_labels', 'arms': {}}
    stop_times = {(r['payload']['run_id'], r.get('console_boot_id', 'unknown')): r['received_mono_ms']
                  for r in rows if r['channel'] == 'commands' and r['payload'].get('op') == 'stop'}
    for arm in sorted({r['arm'] for r in trials}):
        arm_reports = [r for r in reports if r['arm'] == arm]
        arm_ids = {r['run_id'] for r in arm_reports}
        arm_rows = [r for r in rows if r['payload']['run_id'] in arm_ids]
        timely = [r for r in arm_rows if r['received_mono_ms'] <= stop_times.get(
            (r['payload']['run_id'], r.get('console_boot_id', 'unknown')), float('inf'))]
        observations = [r['payload'] for r in timely if r['channel'] == 'observations']
        tracks = [t for r in observations for t in r.get('tracks', [])]
        transport = [r['payload'] for r in arm_rows if r['payload'].get('kind') == 'transport_stats']
        average = lambda key: statistics.mean(r[key] for r in arm_reports if r[key] is not None)
        stats = {key: average(key) for key in ['observed_delivery_hz', 'infer_p50_ms', 'infer_p95_ms',
                 'start_to_first_observation_ms', 'start_to_first_llm_ms']}
        for key in ['frame_bytes', 'observation_bytes', 'preview_bytes']:
            stats['pi_' + key + '_per_second'] = statistics.mean(r[key] / r['duration_s'] for r in transport)
        stats.update(runs=len(arm_reports), observations=sum(r['observations'] for r in arm_reports),
                     max_people_per_observation=max((len(r['tracks']) for r in observations), default=0),
                     mean_people_per_observation=statistics.mean(len(r['tracks']) for r in observations),
                     over_three_people_observations=sum(len(r['tracks']) > 3 for r in observations),
                     track_observations=len(tracks),
                     unknown_action_fraction=sum(t['action']['value'] == 'unknown' for t in tracks) / len(tracks) if tracks else None,
                     action_counts=dict(Counter(t['action']['value'] for t in tracks)),
                     clothing_nonempty_track_observations=sum(bool(t['clothing']) for t in tracks),
                     numerical_clo_met_track_observations=sum(t['clo'] is not None and t['met'] is not None for t in tracks),
                     llm_status_counts=dict(Counter(r['payload'].get('status') for r in timely if r['channel'] == 'llm')),
                     runtime_failure_events=sum(r['payload'].get('kind') in ('failed', 'decode_failed') for r in arm_rows),
                     late_observations=sum(r['late_observations_after_stop'] for r in arm_reports),
                     late_llm=sum(r['late_llm_after_stop'] for r in arm_reports),
                     max_observation_gap_ms=max(r['max_observation_gap_ms'] for r in arm_reports),
                     gate_positive=sum(r['gate_positive'] for r in transport),
                     sampled=sum(r['sampled'] for r in transport),
                     publish_failures=sum(r['publish_failures'] for r in transport),
                     config_hashes=sorted({r['payload']['config_hash'] for r in arm_rows if r['payload'].get('kind') == 'vision_ready'}),
                     contracts=[r['payload'] for r in arm_rows if r['payload'].get('kind') == 'vision_ready'])
        result['arms'][arm] = stats
    Path(str(output_prefix) + '-summary.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2) + '\n')
    Path(str(output_prefix) + '-aggregate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('events', type=Path)
    p.add_argument('matrix', type=Path)
    p.add_argument('--output-prefix', type=Path, required=True)
    a = p.parse_args()
    rows = [json.loads(l) for l in a.events.read_text().splitlines() if l.strip()]
    result = aggregate(rows, json.loads(a.matrix.read_text()), a.output_prefix)
    print(json.dumps({k: {s: v for s, v in x.items() if s != 'contracts'}
                      for k, x in result['arms'].items()}, ensure_ascii=False, indent=2))
