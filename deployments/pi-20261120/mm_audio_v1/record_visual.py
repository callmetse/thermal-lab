"""Record ABC visual observations during a test window (Pi side, read-only).

Needed as ground truth for the audio<->vision correlation: the module itself
only keeps the latest observation, so without this we could not say what the
camera actually saw while the user stood / walked / sat down.

Two lessons from the first P3 attempt are baked in:
  * ABC may gzip observations — the compressed ones are the larger, track-bearing
    frames, so parsing without gzip silently produced a 100% empty-candidate log;
  * parse failures are written as an error line instead of being swallowed.

Reads the existing ABC observation topic only; writes nothing into ABC.
"""
import gzip
import json
import sys
import time

from common import ROOT, mqtt

run_id = sys.argv[1]
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 120
out = ROOT / 'data' / 'visual'
out.mkdir(parents=True, exist_ok=True)
path = out / ('%s.jsonl' % run_id)
handle = open(path, 'a', encoding='utf8')
started = time.time()
stats = {'raw': 0, 'gzip': 0, 'parsed': 0, 'errors': 0, 'other_run': 0}


def on_message(client, userdata, msg):
    stats['raw'] += 1
    try:
        payload = msg.payload
        if payload[:2] == b'\x1f\x8b':
            payload = gzip.decompress(payload)
            stats['gzip'] += 1
        x = json.loads(payload)
        stats['parsed'] += 1
    except Exception as ex:
        stats['errors'] += 1
        handle.write(json.dumps({'error': str(ex)}, ensure_ascii=False) + '\n')
        handle.flush()
        return
    if x.get('run_id') != run_id:
        stats['other_run'] += 1
        return
    tracks = x.get('tracks') or []
    handle.write(json.dumps({
        'wall_ms': time.time() * 1000,
        't': round(time.time() - started, 2),
        'frame': x.get('source_frame_id'),
        'people': len(tracks),
        'actions': [(t.get('action') or {}).get('value') for t in tracks],
        'postures': [(t.get('posture') or {}).get('state') for t in tracks],
        'status': x.get('status'),
    }, ensure_ascii=False) + '\n')
    handle.flush()


client = mqtt('visualrec', on_message, ['labs/abc20261102/v1/runs/+/observations'])
deadline = time.time() + seconds
while time.time() < deadline:
    time.sleep(0.5)
client.loop_stop()
handle.close()
print('wrote %s stats=%s' % (path, stats), flush=True)
