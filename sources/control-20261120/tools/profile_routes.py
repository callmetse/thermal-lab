"""Same-video control/media-path ablation with warmup, real network counters and no training."""
import argparse
import gzip
import json
from pathlib import Path
import shlex
import time
from ssh_access import connection

ROOT=Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser();p.add_argument('--cases',default='control20,direct20,batch20,batch30');p.add_argument('--repeats',type=int,default=1);p.add_argument('--tag',default='routes');a=p.parse_args()
base=Path('/mnt/c/Users/operator/.ssh');known=base/'known_hosts';records=[]
phases={'control20':(False,False,20,0),'direct20':(True,False,20,0),'batch20':(True,True,20,0),'batch30':(True,True,30,0),'batch24':(True,True,24,0),'ack20':(True,True,20,4),'ack24':(True,True,24,4)}

def checked(run,command,data=None,timeout=30):
    for attempt in range(3):
        r=run(command,data=data,timeout=timeout)
        if r.returncode==0:return r.stdout
        if r.returncode!=255:break
        time.sleep(1)
    raise RuntimeError('Remote experiment step failed: '+command)

with connection('192.0.2.11','pi',22,base/'id_ed25519',known) as pi,connection('gpu.example.invalid','root',17005,base/'autodl_yolo_17005',known) as gpu,connection('192.0.2.10','root',22,base/'control-example',known) as control:
    def api(name,body=None):
        command='python3 /srv/abc20261102/tools/control_api.py '+name
        if body is not None:command+=' '+shlex.quote(json.dumps(body))
        return json.loads(checked(control,command))
    if api('state').get('active'):raise RuntimeError('A user experiment is active; not replacing it')
    original=json.loads((ROOT/'config.json').read_text())
    def network():
        code="import json;from pathlib import Path;print(json.dumps({p.parent.name:{k:int((p/k).read_text()) for k in ['tx_bytes','rx_bytes']} for p in Path('/sys/class/net').glob('*/statistics') if p.parent.name!='lo'}))"
        return json.loads(checked(control,'python3 -c '+shlex.quote(code)))
    for name in a.cases.split(','):
        direct,batch,fps,ack_window=phases[name];c={**original,'capture_fps':30,'sample_fps':fps,'preview_hz':12,'preview_max_side':480,'preview_jpeg_quality':35,'media_enabled':direct,'cloud_action_batch':batch,'media_ack_window':ack_window}
        blob=json.dumps(c,indent=2).encode();(ROOT/'results/profiles/performance'/f'{name}-config.json').write_bytes(blob)
        for run,root,role in [(pi,'/home/pi/abc20261102','edge'),(gpu,'/root/abc20261102','cloud'),(control,'/srv/abc20261102','')]:
            checked(run,'cat > '+root+'/config.json',blob)
            if role:checked(run,root+'/.venv/bin/supervisorctl -c '+root+'/deploy/supervisord.ini restart vision-'+role)
        for repeat in range(a.repeats+1):
            before=network();start=time.monotonic();run=api('start',{'arm':'C','source':'replay','seconds':15})
            try:time.sleep(15)
            finally:api('stop',{})
            after=network();duration=time.monotonic()-start
            row={'phase':name,'repeat':repeat,'warmup':repeat==0,'run_id':run['run_id'],'arm':'C','source':'same unlabelled replay','target_sample_fps':fps,'preview_target_hz':12,'media_route':'direct_gpu' if direct else 'control_cloud','action_batch':batch,'media_ack_window':ack_window,'control_network_before':before,'control_network_after':after,'counter_interval_s':duration}
            records.append(row);(ROOT/f'results/performance-{a.tag}-runs.json').write_text(json.dumps(records,indent=2));print(json.dumps({k:v for k,v in row.items() if not k.startswith('control_network')}),flush=True);time.sleep(1)
    ids=[r['run_id'] for r in records]
    code="import gzip,json,sys;from pathlib import Path;ids=set("+repr(ids)+");lines=[l.strip() for l in Path('/srv/abc20261102/data/events.jsonl').open() if json.loads(l)['payload'].get('run_id') in ids];sys.stdout.buffer.write(gzip.compress((chr(10).join(lines)+chr(10)).encode()))"
    data=checked(control,'python3 -c '+shlex.quote(code),timeout=40);(ROOT/f'results/events-performance-{a.tag}.jsonl').write_bytes(gzip.decompress(data))
    print('Route comparison events collected; last profile remains loaded for review',flush=True)
