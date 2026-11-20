"""Deploy only the isolated project, with an idle check and per-host rollback archive."""
import io
import json
from pathlib import Path
import shlex
import tarfile
import time
from ssh_access import connection
ROOT=Path(__file__).resolve().parent.parent
KEYS=Path('/mnt/c/Users/operator/.ssh')
HOSTS={
 'pi':('192.0.2.11','pi',22,'id_ed25519','/home/pi/abc20261102'),
 'control':('192.0.2.10','root',22,'control-example','/srv/abc20261102'),
 'gpu':('gpu.example.invalid','root',22,'gpu-example','/root/abc20261102')}
FILES=['config.json','lab/common.py','lab/worker.py','lab/local_preview.py','lab/result_codec.py','lab/presentation.py','lab/llm.py','tools/build_flows.py','tools/export_results.py','deploy/nodered/page.html','deploy/nodered/settings.js','deploy/console.Caddyfile','requirements-runtime.txt','deploy/mosquitto-native.conf','tools/build_supervisor.py','tools/start_gpu_native.py']
def remote(role,command,data=None,timeout=50):
 host,user,port,key,root=HOSTS[role]
 for attempt in range(3):
  with connection(host,user,port,KEYS/key,KEYS/'known_hosts') as run:
   r=run(command,data=data,timeout=timeout)
  if r.returncode!=255:break
  time.sleep(1)
 if r.returncode:raise RuntimeError(role+': '+r.stderr.decode(errors='replace')[-800:]+r.stdout.decode(errors='replace')[-800:])
 return r.stdout

def main():
 state=json.loads(remote('control','python3 /srv/abc20261102/tools/control_api.py state'))
 if state.get('active'):raise RuntimeError('An experiment is active; refusing deployment')
 buf=io.BytesIO()
 with tarfile.open(fileobj=buf,mode='w:gz') as archive:
  for f in FILES:archive.add(ROOT/f,arcname=f)
 stamp=time.strftime('%Y%m%d-%H%M%S')
 for role in ('gpu','pi','control'):
  root=HOSTS[role][-1]
  remote(role,'cd '+root+' && mkdir -p state/backups && tar -czf state/backups/before-local-preview-'+stamp+'.tar.gz config.json lab tools/build_flows.py deploy')
  if role in ('gpu','pi'):
   supervisor=root+'/.venv/bin/supervisorctl -c '+root+'/deploy/supervisord.ini '
   remote(role,supervisor+'stop '+('vision-cloud llm-adapter' if role=='gpu' else 'vision-edge'))
  remote(role,'cd '+root+' && tar -xzf -',data=buf.getvalue())
  if role in ('gpu','pi'):
   remote(role,'cd '+root+' && .venv/bin/python -m compileall -q lab')
   remote(role,supervisor+'start '+('vision-cloud llm-adapter' if role=='gpu' else 'vision-edge'))
   if role=='pi':
    remote(role,'sleep 2 && curl -fsS http://127.0.0.1:28990/health')
    remote(role,'set -a; . /home/pi/.config/abc20261102/console-auth.env; set +a; '+root+'/runtime/viewer/caddy validate --config '+root+'/deploy/console.Caddyfile --adapter caddyfile')
    remote(role,supervisor+'restart console-gateway')
  else:
   remote(role,'cd '+root+' && python3 tools/build_flows.py --native && systemctl restart abc20261102-broker abc20261102-console')
  print(role+' deployed',flush=True)
 print(json.dumps({'backup_stamp':stamp,'files':FILES}))
if __name__=='__main__':main()
