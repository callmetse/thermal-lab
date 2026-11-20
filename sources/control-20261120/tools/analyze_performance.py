"""Join console-clock evidence and passive browser counters without clock correction."""
import argparse
import json
from pathlib import Path

from report import percentile, summarize

ROOT = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument('--tag', default='routes')
p.add_argument('--browser', default='perf-browser-runs.json')
a = p.parse_args()
runs = json.loads((ROOT / f'results/performance-{a.tag}-runs.json').read_text())
rows = [json.loads(line) for line in (ROOT / f'results/events-performance-{a.tag}.jsonl').read_text().splitlines() if line.strip()]
browser = json.loads((ROOT / 'results' / a.browser).read_text())
reports = {r['run_id']: r for r in summarize(rows)}


def rate(times):
    return (len(times) - 1) * 1000 / (max(times) - min(times)) if len(times) > 1 and max(times) > min(times) else None


out = []
for run in runs:
    rid = run['run_id']
    r = reports[rid]
    selected = [row for row in rows if row['payload'].get('run_id') == rid and row.get('console_boot_id', 'unknown') == r['console_boot_id']]
    stop = min((row['received_mono_ms'] for row in selected if row['channel'] == 'commands' and row['payload'].get('op') == 'stop'), default=float('inf'))
    obs = sorted((row for row in selected if row['channel'] == 'observations' and row['received_mono_ms'] <= stop), key=lambda row: row['received_mono_ms'])
    lag = [(row['received_mono_ms'] - obs[0]['received_mono_ms']) - (row['payload']['source_pts_s'] - obs[0]['payload']['source_pts_s']) * 1000 for row in obs] if obs else []
    queue = [row['payload']['timing']['cloud_queue_ms'] for row in obs if isinstance(row['payload'].get('timing', {}).get('cloud_queue_ms'), (int, float))]
    b = browser['runs'].get(rid, {})
    times = b.get('images', [])
    delta = lambda key: sum(v[key] - run['control_network_before'][k][key] for k, v in run['control_network_after'].items() if k in run['control_network_before']) / run['counter_interval_s']
    transport = r.pop('transport')
    out.append({**run, **r,
                'browser_draw_hz': rate(times), 'browser_drawn_images': len(times),
                'browser_unique_observation_hz': rate(list(b.get('observations', {}).values())),
                'browser_max_boxes': b.get('max_boxes'), 'browser_max_draw_gap_ms': max((b-a for a,b in zip(times,times[1:])), default=None),
                'relative_lag_end_ms': lag[-1] if lag else None,
                'relative_lag_p95_ms': percentile(lag, .95),
                'relative_lag_range_ms': max(lag)-min(lag) if lag else None,
                'cloud_app_queue_p95_ms': percentile(queue, .95),
                'control_host_tx_bytes_s': delta('tx_bytes'), 'control_host_rx_bytes_s': delta('rx_bytes'),
                'runtime_failure_events': sum(row['payload'].get('kind') in ('failed', 'decode_failed') for row in selected),
                'cloud_stats': [row['payload'] for row in selected if row['payload'].get('kind') == 'cloud_stats'],
                'edge_transport': [{**t, 'frame_bytes_s': t['frame_bytes']/t['duration_s']} for t in transport]})

output = {'method': 'Each run is separate; warmups retained but excluded from comparisons. Delivery Hz excludes startup/EOF idle. '
                    'Relative lag is receive elapsed minus source elapsed, relative to first result, not absolute AoI. '
                    'Host byte counters include SSH and all host traffic. Browser image.onload plus double RAF counts updates, not monitor scanout.',
          'browser_errors': browser['errors'], 'runs': out}
(ROOT / f'results/performance-{a.tag}-analysis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
for r in out:
    print(json.dumps({k:r[k] for k in ['phase','repeat','run_id','observed_delivery_hz','browser_draw_hz','infer_p50_ms','infer_p95_ms','relative_lag_end_ms','relative_lag_p95_ms','observations','late_observations_after_stop','control_host_tx_bytes_s','runtime_failure_events']}, ensure_ascii=False))
