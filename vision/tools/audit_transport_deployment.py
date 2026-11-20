"""Read actual isolated runtime files, dependencies and service states; no restarts."""
import hashlib
import json
from pathlib import Path
import shlex
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from deploy_transport_local import remote, HOSTS, FILES
ROOT=Path(__file__).resolve().parent.parent
FILES=sorted(set(FILES+['PROVENANCE.json']+[str(p.relative_to(ROOT)) for p in (ROOT/'lab').glob('*.py')]+[str(p.relative_to(ROOT)) for p in (ROOT/'vendor/algorithm').glob('*.py')]))
CODE=r'''
import json,hashlib,subprocess,sys
from pathlib import Path
root=Path(ROOT_VALUE);files=FILE_VALUES;role=ROLE_VALUE
out={'python':sys.version,'hashes':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() if (root/f).is_file() else None for f in files},'checks':[]}
def check(args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=40);out['checks'].append({'command':args,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
if role in ('pi','gpu'):
 check([str(root/'.venv/bin/python'),'-m','pip','freeze'])
 check([str(root/'.venv/bin/supervisorctl'),'-c',str(root/'deploy/supervisord.ini'),'status'])
if role=='pi':
 check(['systemctl','--user','is-active','edge-bridge-v4','edge-mqtt-tunnel'])
 check(['systemctl','--user','is-enabled','edge-bridge-v4','edge-mqtt-tunnel'])
 check(['curl','-fsS','http://127.0.0.1:28990/health'])
if role=='control':
 check(['systemctl','is-active','mosquitto','nodered'])
 check(['systemctl','is-enabled','mosquitto','nodered'])
 check(['systemctl','is-active','abc20261102-broker','abc20261102-console','abc20261102-gpu-ensure.timer'])
 check(['python3',str(root/'tools/control_api.py'),'state'])
if role=='gpu':
 check(['nvidia-smi','--query-gpu=name,memory.total','--format=csv,noheader'])
 check(['curl','-fsS','http://127.0.0.1:28434/api/tags'])
print(json.dumps(out))
'''
def audit(role):
 root=HOSTS[role][-1];py=root+'/.venv/bin/python' if role!='control' else 'python3'
 code=CODE.replace('ROOT_VALUE',repr(root)).replace('FILE_VALUES',repr(FILES)).replace('ROLE_VALUE',repr(role))
 return role,json.loads(remote(role,py+' -c '+shlex.quote(code),timeout=100))
def main():
 manifest={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in FILES}
 result={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'Read-only live deployment audit; hashes for shared runtime sources, not generated host-specific paths','local':manifest,'hosts':{}}
 with ThreadPoolExecutor() as pool:
  for role,value in pool.map(audit,HOSTS):
   value['differences']=[f for f in FILES if manifest[f]!=value['hashes'][f]];result['hosts'][role]=value
   for check in value['checks']:
    if '-m' in check['command'] and 'freeze' in check['command']:
     (ROOT/f'results/environment-freeze-transport-{role}.txt').write_text(check['stdout'])
   print(role,'source differences:',value['differences'])
 (ROOT/'results/transport-final-deployment-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 if any(h['differences'] for h in result['hosts'].values()):raise RuntimeError('Source drift detected')
 old=ROOT/'results/current-source-manifest.json'
 if old.exists() and not (ROOT/'results/source-manifest-before-transport-final.json').exists():
  (ROOT/'results/source-manifest-before-transport-final.json').write_bytes(old.read_bytes())
 old.write_text(json.dumps(manifest,indent=2)+'\n')
if __name__=='__main__':main()
