"""Control-host replay/camera trials through actual HTTP/MQTT; no fake ACKs."""
import argparse
import base64
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seconds', type=int, default=15)
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--tag', default='smoke')
    p.add_argument('--arms', default='C,B,A')
    p.add_argument('--source', choices=['replay', 'camera'], default='replay')
    a = p.parse_args()
    creds = json.loads(Path('/etc/abc20261102/ui-login.json').read_text())
    auth = 'Basic ' + base64.b64encode((creds['user'] + ':' + creds['password']).encode()).decode()
    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request('http://127.0.0.1:28880/api/' + path, data=data,
                                     headers={'Authorization': auth, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    # A systemd process can be active before Node-RED has bound its HTTP port.
    for attempt in range(30):
        try:
            initial = request('state')
            break
        except urllib.error.URLError:
            if attempt == 29:
                raise
            time.sleep(.5)
    if initial.get('active'):
        raise RuntimeError('An independent-lab run is already active; refusing to interrupt it')
    output = []
    arms = a.arms.split(',')
    for repeat in range(a.repeats):
        order = arms[repeat % len(arms):] + arms[:repeat % len(arms)]
        for arm in order:
            run = request('start', {'arm': arm, 'source': a.source, 'seconds': a.seconds})
            states = []
            try:
                end = time.monotonic() + a.seconds
                while time.monotonic() < end:
                    states.append(request('state'))
                    time.sleep(.5)
            finally:
                request('stop', {})
            row = {'run_id': run['run_id'], 'arm': arm, 'tag': a.tag, 'repeat': repeat,
                   'requested_seconds': a.seconds, 'snapshots': states,
                   'browser_render_measured': False,
                   'source': 'physical_camera' if a.source == 'camera' else 'copied_unlabelled_video'}
            (ROOT / 'results' / (a.tag + '_' + run['run_id'] + '.json')).write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
            output.append({k: row[k] for k in ['run_id', 'arm', 'tag', 'repeat']})
            print(json.dumps(output[-1]), flush=True)
            time.sleep(1)
    (ROOT / 'results' / (a.tag + '_matrix.json')).write_text(json.dumps(output, indent=2) + '\n')


if __name__ == '__main__':
    main()
