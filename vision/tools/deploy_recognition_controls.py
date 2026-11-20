"""Deploy reviewed per-run region/confidence/unlimited controls with backups.

Only explicitly enumerated visual files and Node-RED functions are changed.
Runtime configuration, credentials, audio and environment services are untouched.
"""
import hashlib,json
from pathlib import Path
from e2e_collaboration import remote,copy

ROOT=Path(__file__).resolve().parent.parent
EVIDENCE=Path('D:/thermal-lab-archive/05_实测结果/e2e_20261116_1605')
VERSION='recognition-controls-20261116-1620'
FILES=['lab/common.py','lab/worker.py','lab/vision.py','lab/presentation.py',
       'lab/recognition_region.py','vendor/algorithm/temporal_v1/track_video.py']


def main():
    manifests={}
    for host,base,role in [('pi','/home/pi/abc20261102','vision-edge'),('gpu','/root/abc20261102','vision-cloud')]:
        stage=base+'/state/'+VERSION+'/incoming'
        remote(host,f"from pathlib import Path;[(Path({stage!r})/p).parent.mkdir(parents=True,exist_ok=True) for p in {FILES!r}]")
        expected={}
        for rel in FILES:
            copy(host,ROOT/rel,stage+'/'+rel)
            expected[rel]=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
        source=f'''import ast,hashlib,json,pathlib,shutil,os,subprocess
root=pathlib.Path({base!r});stage=pathlib.Path({stage!r});backup=root/'state'/{VERSION!r}/'before'
files={FILES!r};expected={expected!r};manifest={{'before':{{}},'after':{{}}}}
for rel in files:
 p=stage/rel;assert hashlib.sha256(p.read_bytes()).hexdigest()==expected[rel];ast.parse(p.read_text())
for rel in files:
 target=root/rel; saved=backup/rel;saved.parent.mkdir(parents=True,exist_ok=True)
 if target.exists():
  if saved.exists():raise RuntimeError('Backup already exists: '+rel)
  shutil.copy2(target,saved);manifest['before'][rel]=hashlib.sha256(target.read_bytes()).hexdigest()
 else:manifest['before'][rel]=None
 shutil.copy2(stage/rel,target.with_suffix('.deploy_tmp'));os.replace(str(target.with_suffix('.deploy_tmp')),str(target))
 manifest['after'][rel]=hashlib.sha256(target.read_bytes()).hexdigest()
(backup.parent/'manifest.json').write_text(json.dumps(manifest,indent=2))
subprocess.run([str(root/'.venv/bin/supervisorctl'),'-c',str(root/'deploy/supervisord.ini'),'restart',{role!r}],check=True)
print(json.dumps(manifest))
'''
        manifests[host]=remote(host,source,timeout=60)
        print(host,'deployed')
    base='/srv/abc20261102';stage=base+'/state/'+VERSION
    remote('control',f"from pathlib import Path;Path({stage!r}).mkdir(parents=True,exist_ok=True)")
    copy('control',ROOT/'deploy/nodered/flows-native.json',stage+'/generated.json')
    script=f'''import pathlib,json,shutil,subprocess,sys,os
root=pathlib.Path({base!r});stage=pathlib.Path({stage!r});sys.path.insert(0,str(root/'tools'))
from control_api import request
assert not request('state').get('active'),'Cannot restart console during an active run'
flow=root/'deploy/nodered/flows-native.json';settings=root/'deploy/nodered/settings.js'
for p in [flow,settings]:
 saved=stage/(p.name+'.before');assert not saved.exists();shutil.copy2(p,saved)
nodes=json.loads(flow.read_text());incoming={{n['id']:n for n in json.loads((stage/'generated.json').read_text())}}
changed=[]
for n in nodes:
 if n['id'] in ['page','start','state','live_snapshot','collect','log']:
  new=incoming[n['id']]
  for key in ['func','type','name','outputs']:
   if key in new:n[key]=new[key]
  if n['id']=='log':
   for key in ['filename','filenameType','appendNewline','overwriteFile']:n.pop(key,None)
  changed.append(n['id'])
assert set(changed)=={{'page','start','state','live_snapshot','collect','log'}}
tmp=flow.with_suffix('.tmp');tmp.write_text(json.dumps(nodes,ensure_ascii=False,indent=2));os.replace(str(tmp),str(flow))
s=settings.read_text();assert 'functionGlobalContext: {{crypto:' in s
s=s.replace('functionGlobalContext: {{crypto:',"functionGlobalContext: {{fs: require('fs'), audit_path: require('path').join(process.cwd(),'data/events.jsonl'), crypto:")
settings.write_text(s)
for n in nodes:
 if n.get('type')=='function':subprocess.run(['node','-e',"new Function('msg','node','global','context','flow',process.argv[1])",n['func']],check=True)
subprocess.run(['node','--check',str(settings)],check=True)
subprocess.run(['systemctl','restart','abc20261102-console'],check=True)
print(json.dumps({{'changed':changed,'backup':str(stage)}}))
'''
    manifests['control']=remote('control',script,timeout=60)
    (EVIDENCE/'recognition_controls_deploy.json').write_text(json.dumps(manifests,indent=2),encoding='utf-8')
    print('control deployed; all backups recorded')


if __name__=='__main__':main()
