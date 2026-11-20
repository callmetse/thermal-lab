"""Build transparent transport measurements; do not pool replay/camera or warmups."""
import json
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parent.parent
TAGS=['transport-before-one','transport-before-two','transport-after12-one','transport-after12-two','transport-after20-one','transport-after20-two','transport-local20-one','transport-local-split20-one','transport-local-split12-one','transport-local-split12-two','transport-local-camera20-two','transport-local-camera12-two','transport-local-A','transport-local-B','transport-local-soak120']
rows=[]
for tag in TAGS:
 path=ROOT/f'results/{tag}-browser.json'
 if not path.exists():continue
 data=json.loads(path.read_text())
 events=ROOT/f'results/{tag}-events.jsonl'
 ev=[json.loads(l) for l in events.read_text().splitlines() if l] if events.exists() else []
 for run in data['runs']:
  rid=run.get('run_id'); rr=[r for r in ev if r['payload'].get('run_id')==rid]
  stats=[r['payload'] for r in rr if r['payload'].get('kind')=='result_stats' and r['payload'].get('producer') in ('cloud','edge') and r['payload'].get('generated',0)>0]
  observations=[r for r in rr if r['channel']=='observations']
  record={'tag':tag,'run_id':rid,'warmup':run['warmup'],'source':run.get('source'),'arm':run.get('arm'),'target_sample_fps':run.get('run',{}).get('sample_fps'),
          'received_observations':len(observations),'result_stats':stats,'clients':[]}
  for client in run['clients']:
   record['clients'].append({k:client.get(k) for k in ('effective_draw_stream_fps','whole_window_fps','pi_capture_to_preview_ms','p95_draw_gap_ms','max_draw_gap_ms','same_frame_mismatches','max_boxes','ack_non_200','errors')})
  rows.append(record)
(ROOT/'results/transport-final-summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
for row in rows:
 if not row['warmup']:
  print(row['tag'],row['run_id'],row['clients'],[(s['generated'],s['publish_rejected']) for s in row['result_stats']])
