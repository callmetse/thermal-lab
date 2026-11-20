"""Windows driver for the isolated collaboration smoke run; no production model replacement.

Remote destinations and identity file paths are private deployment metadata, not
credential contents. Every remote Python script is sent on stdin, without shell
interpolation of its source. Capture inputs stay under a versioned run directory.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import time

SSH = 'C:/Windows/System32/OpenSSH/ssh.exe'
SCP = 'C:/Windows/System32/OpenSSH/scp.exe'
HOSTS = {
    'pi': ('pi@192.0.2.11', 22, 'C:/Users/operator/.ssh/id_ed25519', '/home/pi/abc20261102/.venv/bin/python'),
    'control': ('root@192.0.2.10', 22, 'C:/Users/operator/.ssh/control-example', 'python3'),
    'gpu': ('root@gpu.example.invalid', 22, 'C:/Users/operator/.ssh/gpu-example', '/root/abc20261102/.venv/bin/python'),
}


def remote(host, source, timeout=90):
    dest, port, key, python = HOSTS[host]
    result = subprocess.run([SSH, '-F', 'NUL', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                             '-p', str(port), '-i', key, dest, python + ' -'],
                            input=source, text=True, encoding='utf-8', capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{host} exit={result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-4000:]}')
    return result.stdout


def copy(host, local, remote_path, download=False, recursive=False):
    dest, port, key, _ = HOSTS[host]
    args = [SCP, '-F', 'NUL', '-o', 'BatchMode=yes', '-P', str(port), '-i', key]
    if recursive:
        args.append('-r')
    args += [f'{dest}:{remote_path}', str(local)] if download else [str(local), f'{dest}:{remote_path}']
    subprocess.run(args, check=True, timeout=180, capture_output=True)


def capture(out, name, source, seconds, overrides=None, observe_seconds=None):
    # Capture full observations, model evidence and LLM events, not track counts.
    pi_path = f'/home/pi/abc20261102/state/{out.name}/{name}.json'
    script = '''import gzip,json,pathlib,time,uuid,urllib.request,threading
import paho.mqtt.client as m
result={'messages':[], 'errors':[], 'clock':'Pi monotonic, full MQTT payloads'}
started=time.monotonic_ns(); ready=threading.Event()
def connected(c,u,f,r,p):
 c.subscribe('labs/abc20261102/v1/#',qos=1);ready.set()
def received(c,u,msg):
 try:
  b=msg.payload
  if msg.topic.endswith(('/frames','/preview')): return
  if b[:2]==b'\\x1f\\x8b':b=gzip.decompress(b)
  body=json.loads(b)
  result['messages'].append({'topic':msg.topic,'received_mono_ns':time.monotonic_ns(), 'body':body})
 except Exception as e:result['errors'].append(type(e).__name__+':'+str(e))
c=m.Client(m.CallbackAPIVersion.VERSION2,client_id='e2e-'+uuid.uuid4().hex[:8],protocol=m.MQTTv5)
c.on_connect=connected;c.on_message=received;c.connect('127.0.0.1',28883);c.loop_start()
assert ready.wait(8),'broker not connected'
time.sleep(DURATION)
c.disconnect();c.loop_stop()
for key,url in [('environment','http://127.0.0.1:28984/env/v1/latest?zone_id=lab'),('environment_health','http://127.0.0.1:28984/env/v1/health')]:
 try: result[key]=json.load(urllib.request.urlopen(url,timeout=4))
 except Exception as e:result[key]={'error':str(e)}
path=pathlib.Path(OUTPUT);path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'saved':str(path),'messages':len(result['messages']),'errors':result['errors']}))
'''.replace('DURATION', str((observe_seconds or seconds)+18)).replace('OUTPUT', repr(pi_path))
    run={'arm':'C','source':source,'seconds':seconds,'sample_fps':12,**(overrides or {})}
    command = f'''import sys,json
sys.path.insert(0,'/srv/abc20261102/tools')
from control_api import request
state=request('state')
if state.get('active'): raise RuntimeError('Another run is active; do not interrupt')
print(json.dumps(request('start',{run!r})))
'''
    with concurrent.futures.ThreadPoolExecutor() as pool:
        task = pool.submit(remote, 'pi', script, (observe_seconds or seconds)+40)
        time.sleep(3)
        started_run_id=None
        try:
            response = remote('control', command)
            started_run_id=json.loads(response).get('run_id')
            (out/f'{name}_start.json').write_text(response, encoding='utf-8')
        finally:
            try:
                log = task.result()
                (out/f'{name}_capture_log.txt').write_text(log, encoding='utf-8')
            finally:
                if run.get('unlimited') and started_run_id:
                    stop_script=("import sys,json;sys.path.insert(0,'/srv/abc20261102/tools');from control_api import request;"
                                 "active=request('state').get('active');"
                                 "print(json.dumps(request('stop',{}) if active and active['run_id']=="+repr(started_run_id)+" else {'status':'already_stopped_or_superseded'}))")
                    (out/f'{name}_stop.json').write_text(remote('control',stop_script),encoding='utf-8')
    copy('pi', out/f'{name}.json', pi_path, download=True)
    data = json.loads((out/f'{name}.json').read_text(encoding='utf-8'))
    observed = [m['body'] for m in data['messages'] if m['topic'].endswith('/observations')]
    llm = [m['body'] for m in data['messages'] if m['topic'].endswith('/llm')]
    e2e = [(m['received_mono_ns']-m['body']['frame_available_mono_ns'])/1e6
           for m in data['messages'] if m['topic'].endswith('/observations')
           and m['body'].get('producer') == 'cloud' and 'frame_available_mono_ns' in m['body']]
    stats = {'observations': len(observed), 'cloud_results': sum(o.get('producer') == 'cloud' for o in observed),
             'with_tracks': sum(bool(o.get('tracks')) for o in observed), 'llm_results': len(llm),
             'llm_generated': sum(o.get('status') == 'generated' for o in llm),
             'runs': sorted({o['run_id'] for o in observed}),
             'pi_roundtrip_ms_p50': sorted(e2e)[len(e2e)//2] if e2e else None,
             'pi_roundtrip_ms_p95': sorted(e2e)[min(len(e2e)-1,int(len(e2e)*.95))] if e2e else None}
    (out/f'{name}_summary.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(name, json.dumps(stats))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--phase', choices=['capture', 'export'], required=True)
    args = ap.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    if args.phase == 'capture':
        capture(out, 'camera', 'camera', 25)
        capture(out, 'replay', 'replay', 15)
    elif args.phase == 'export':
        target = f'/home/pi/abc20261102/state/{out.name}/hard_examples_outbox'
        result = remote('pi', f'''import subprocess
subprocess.run(['/home/pi/abc20261102/.venv/bin/python','/home/pi/abc20261102/tools/export_hard_examples.py','--out',{target!r}],check=True)
''')
        (out/'export_log.txt').write_text(result, encoding='utf-8')
        copy('pi', out, target, download=True, recursive=True)
        package = out/'hard_examples_outbox'
        records = [json.loads(s) for s in (package/'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
        for rec in records:
            media = package/'samples'/f"{rec['sample_id']}.jpg"
            assert hashlib.sha256(media.read_bytes()).hexdigest() == rec['media_sha256']
        print(json.dumps({'exported_samples':len(records), 'all_hashes_verified':True}))


if __name__ == '__main__':
    main()
