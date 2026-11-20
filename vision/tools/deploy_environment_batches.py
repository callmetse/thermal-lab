"""Deploy environment UI and batch archiving, preserving the sensor service."""
import ast,hashlib,json
from pathlib import Path
from e2e_collaboration import remote,copy

ROOT=Path(__file__).resolve().parent.parent
VERSION='environment-batches-20261116-1700'
OUT=Path('D:/thermal-lab-archive/05_实测结果/e2e_20261116_1605')


def main():
    reports={}
    for host,root,files in [
        ('pi','/home/pi/abc20261102',['lab/common.py','lab/worker.py','lab/presentation.py','lab/local_preview.py','lab/collection_batches.py','lab/collection_routes.py']),
        ('gpu','/root/abc20261102',['lab/common.py','lab/worker.py','lab/presentation.py'])]:
        stage=root+'/state/'+VERSION+'/incoming'
        remote(host,f"from pathlib import Path;[(Path({stage!r})/f).parent.mkdir(parents=True,exist_ok=True) for f in {files!r}]")
        for f in files:copy(host,ROOT/f,stage+'/'+f)
        reports[host]=remote(host,f'''import pathlib,ast,shutil,hashlib,json,subprocess,os
r=pathlib.Path({root!r});stage=pathlib.Path({stage!r});before=stage.parent/'before';manifest={{}}
for f in {files!r}:ast.parse((stage/f).read_text())
for f in {files!r}:
 p=r/f;b=before/f;b.parent.mkdir(parents=True,exist_ok=True);assert not b.exists()
 old=None
 if p.exists():old=hashlib.sha256(p.read_bytes()).hexdigest();shutil.copy2(p,b)
 shutil.copy2(stage/f,p.with_suffix('.batch_tmp'));os.replace(str(p.with_suffix('.batch_tmp')),str(p))
 manifest[f]={{'before':old,'after':hashlib.sha256(p.read_bytes()).hexdigest()}}
if {host!r}=='pi':
 p=r/'deploy/console.Caddyfile';shutil.copy2(p,before/'console.Caddyfile')
 lines=p.read_text().splitlines();changed=False
 for i,line in enumerate(lines):
  if line.strip().startswith('@preview path '):
   lines[i]=line+' /api/environment/* /api/collection-batches /api/collection-batches/*';changed=True
 assert changed;p.write_text('\\n'.join(lines)+'\\n')
roles=['vision-edge','console-gateway'] if {host!r}=='pi' else ['vision-cloud','llm-adapter']
for role in roles:subprocess.run([str(r/'.venv/bin/supervisorctl'),'-c',str(r/'deploy/supervisord.ini'),'restart',role],check=True)
(stage.parent/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest))
'''.replace('\n+','\n'),timeout=60)
        print(host,'environment metadata deployed')
    root='/srv/abc20261102';stage=root+'/state/'+VERSION
    remote('control',f"from pathlib import Path;Path({stage!r}).mkdir(parents=True,exist_ok=True)")
    copy('control',ROOT/'deploy/nodered/flows-native.json',stage+'/generated.json')
    reports['control']=remote('control',f'''import pathlib,json,shutil,subprocess,sys
r=pathlib.Path({root!r});s=pathlib.Path({stage!r});sys.path.insert(0,str(r/'tools'))
from control_api import request
assert not request('state').get('active')
p=r/'deploy/nodered/flows-native.json';assert not (s/'flows.before.json').exists();shutil.copy2(p,s/'flows.before.json')
nodes=json.loads(p.read_text());incoming={{n['id']:n for n in json.loads((s/'generated.json').read_text())}}
for n in nodes:
 if n['id'] in ['page','start','state','live_snapshot','collect']:n['func']=incoming[n['id']]['func']
p.write_text(json.dumps(nodes,ensure_ascii=False,indent=2))
subprocess.run(['systemctl','restart','abc20261102-console'],check=True)
print(json.dumps({{'backup':str(s/'flows.before.json')}}))
'''.replace('\n+','\n'),timeout=40)
    (OUT/'environment_batches_deploy.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    print('environment batch UI deployed; env-gateway not restarted')


if __name__=='__main__':main()
