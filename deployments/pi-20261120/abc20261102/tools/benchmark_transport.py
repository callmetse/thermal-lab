"""Read-only browser/network measurement, apart from starting/stopping owned lab runs.

Use the existing browser venv. Each client launches a separate Chrome process;
no local SSH forwarding, configuration changes or service restarts are performed.
"""
import argparse
from contextlib import ExitStack
import gzip
import json
import math
from pathlib import Path
import re
import shlex
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from ssh_access import connection


ROOT = Path(__file__).resolve().parent.parent
PROBE = r"""() => {
  window.transportProbe={capture:false,runId:null,start:0,draws:[],observations:{},visibility:[],lastId:''};
  const p=window.transportProbe;
  p.begin=()=>{p.capture=true;p.runId=null;p.start=performance.now();p.draws=[];p.observations={};p.lastId='';
    p.visibility=[{t:0,state:document.visibilityState}];};
  p.bind=id=>{p.runId=id};
  p.finish=()=>{p.capture=false;return {window_ms:performance.now()-p.start,draws:p.draws,
    observations:p.observations,visibility_events:p.visibility,visibility_end:document.visibilityState,
    hud:document.querySelector('#fps_chip')?.textContent,observation_hud:document.querySelector('#obs_chip')?.textContent,
    match_status:document.querySelector('#match_status')?.textContent,
    active_run:typeof active==='undefined'?null:active?.run_id};};
  document.addEventListener('visibilitychange',()=>{if(p.capture)p.visibility.push({t:performance.now()-p.start,state:document.visibilityState})});
  function inspect(){
    if(p.capture&&typeof displayed!=='undefined'&&displayed&&displayed.id!==p.lastId){
      p.lastId=displayed.id;
      const meta=displayed.meta||{},o=typeof matchedObservation==='function'?matchedObservation():null;
      if(!p.runId||meta.run_id===p.runId){
        const same=!!o&&o.run_id===meta.run_id&&o.source_frame_id===meta.source_frame_id;
        const hashOk=!o||!meta.jpeg_sha256||!o.jpeg_sha256||meta.jpeg_sha256===o.jpeg_sha256;
        const boxes=document.querySelectorAll('#overlay .box-target').length;
        p.draws.push({id:displayed.id,run_id:meta.run_id,source_frame_id:meta.source_frame_id,
          source_pts_s:meta.source_pts_s,t:performance.now()-p.start,boxes,
          same_frame_observation:same,input_hash_matches:hashOk,
          mismatch:(!!o&&!same)||!hashOk||(boxes>0&&!o),visibility:document.visibilityState});
      }
    }
    requestAnimationFrame(inspect);
  }
  requestAnimationFrame(inspect);
  new MutationObserver(()=>{
    if(!p.capture)return;
    try{const o=JSON.parse(document.querySelector('#obs').textContent);
      if(o&&(!p.runId||o.run_id===p.runId)){
        const key=o.run_id+':'+o.source_frame_id;
        if(!Object.prototype.hasOwnProperty.call(p.observations,key))p.observations[key]=performance.now()-p.start;
      }
    }catch(error){}
  }).observe(document.querySelector('#obs'),{childList:true,subtree:true,characterData:true});
}"""


def percentile(values, p):
    if not values:
        return None
    values=sorted(values);point=(len(values)-1)*p;lo=int(point)
    return values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(point-lo)


def summarize_probe(probe, run_id):
    draws=[x for x in probe['draws'] if x.get('run_id')==run_id]
    times=[x['t'] for x in draws]
    gaps=[b-a for a,b in zip(times,times[1:])]
    elapsed=probe['window_ms']
    silence=[*gaps]
    if times:
        silence.extend([times[0],max(0,elapsed-times[-1])])
    else:
        silence.append(elapsed)
    observations={k:v for k,v in probe['observations'].items() if k.startswith(run_id+':')}
    return {**probe,'draws':draws,'observations':observations,
            'drawn_images':len(draws),
            'effective_draw_stream_fps':(len(times)-1)*1000/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0] else None,
            'whole_window_fps':len(times)*1000/elapsed if elapsed>0 else None,
            'start_to_first_draw_ms':times[0] if times else None,
            'max_draw_gap_ms':max(gaps,default=None),'p95_draw_gap_ms':percentile(gaps,.95),
            'max_silence_including_window_edges_ms':max(silence,default=None),
            'same_frame_mismatches':sum(x['mismatch'] for x in draws),
            'max_boxes':max((x['boxes'] for x in draws),default=0),
            'browser_unique_observation_count':len(observations),
            'visibility_all_recorded_visible':all(x['state']=='visible' for x in probe['visibility_events'])
                 and probe['visibility_end']=='visible' and all(x['visibility']=='visible' for x in draws)}


def new_wire():
    return {path:{'messages':0,'bytes':0,'binary_messages':0,'text_messages':0} for path in ['/live','/live-video']}


def attach(page, client):
    def opened(ws):
        path=urlsplit(ws.url).path
        if path not in ['/live','/live-video']:
            return
        def received(payload):
            if not client['capture']:
                return
            v=client['wire'][path];v['messages']+=1
            v['bytes']+=len(payload) if isinstance(payload,bytes) else len(payload.encode('utf-8'))
            v['binary_messages' if isinstance(payload,bytes) else 'text_messages']+=1
        ws.on('framereceived',received)
    def requested(request):
        if client['capture'] and request.method=='POST' and urlsplit(request.url).path=='/api/ack':
            row={'requested_ms':(time.monotonic()-client['start'])*1000,'status':None,'failed':None}
            client['acks'].append(row);client['pending_acks'][request]=row
    def responded(response):
        row=client['pending_acks'].get(response.request)
        if row is not None:
            row['status']=response.status
    def failed(request):
        row=client['pending_acks'].get(request)
        if row is not None:
            row['failed']=request.failure
    page.on('websocket',opened)
    page.on('request',requested)
    page.on('response',responded)
    page.on('requestfailed',failed)
    page.on('pageerror',lambda error:client['errors'].append(str(error)))


def checked(run, command, timeout=40):
    result=None
    for attempt in range(3):
        result=run(command,timeout=timeout)
        if result.returncode==0:
            return result.stdout
        if result.returncode!=255:
            break
        time.sleep(1)
    raise RuntimeError('Read-only SSH step failed: '+(result.stderr.decode(errors='replace')[:250] if result else command))


def network_counters(run):
    code="import json;from pathlib import Path;print(json.dumps({p.parent.name:{k:int((p/k).read_text()) for k in ['tx_bytes','rx_bytes']} for p in Path('/sys/class/net').glob('*/statistics') if p.parent.name!='lo'}))"
    return json.loads(checked(run,'python3 -c '+shlex.quote(code)))


def collect_events(run, ids, output):
    code="""import gzip,json,sys
from pathlib import Path
ids=set(%s)
with gzip.GzipFile(fileobj=sys.stdout.buffer,mode='wb') as target:
    for line in Path('/srv/abc20261102/data/events.jsonl').open():
        if json.loads(line)['payload'].get('run_id') in ids:
            target.write(line.encode())
"""%repr(ids)
    output.write_bytes(gzip.decompress(checked(run,'python3 -c '+shlex.quote(code),timeout=60)))


def stop_owned(page, context, base, run_id):
    """Never intentionally stop a run owned by someone else."""
    state=context.request.get(base+'/api/state',timeout=10000)
    if not state.ok:
        raise RuntimeError('Cannot check ownership before stopping')
    active_run=state.json().get('active')
    if not active_run or active_run.get('run_id')!=run_id:
        return {'stopped':False,'reason':'already stopped or another run is active'}
    try:
        page.wait_for_function('(id)=>active?.run_id===id',arg=run_id,timeout=3000)
        page.click('#stop_button',timeout=5000)
        page.wait_for_function('(id)=>active?.run_id!==id',arg=run_id,timeout=10000)
        return {'stopped':True,'method':'page button'}
    except Exception:
        # Page may be broken; recheck before the existing control endpoint fallback.
        state=context.request.get(base+'/api/state',timeout=10000).json()
        if state.get('active',{} ) and state['active'].get('run_id')==run_id:
            response=context.request.post(base+'/api/stop',data={'run_id':run_id},timeout=10000)
            if not response.ok:
                raise RuntimeError('Owned run stop fallback failed')
            return {'stopped':True,'method':'existing HTTP stop after ownership recheck'}
        return {'stopped':False,'reason':'not owned at fallback recheck'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',choices=['camera','replay'],default='camera')
    parser.add_argument('--seconds',type=float,default=15)
    parser.add_argument('--clients',type=int,choices=[1,2],default=1)
    parser.add_argument('--tag',required=True)
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--warmup',action='store_true')
    parser.add_argument('--credentials',type=Path,default=Path('/mnt/c/Users/operator/.codex/secrets/abc20261102-console/login.json'))
    parser.add_argument('--url',default='http://192.0.2.11:28880/lab')
    parser.add_argument('--no-collect-events',action='store_true')
    parser.add_argument('--network-counters',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',args.tag) or not 0<args.seconds<=3580 or args.repeats<1:
        parser.error('Use a filename-safe tag, 0 < seconds <= 3580, repeats >= 1')
    credentials=json.loads(args.credentials.read_text())
    username=credentials.get('username',credentials.get('user'))
    if not username or not credentials.get('password'):
        raise RuntimeError('Credential file lacks username/user or password')
    base=args.url.split('/lab',1)[0].rstrip('/')
    output=ROOT/f'results/{args.tag}-browser.json';output.parent.mkdir(exist_ok=True)
    result={'tag':args.tag,'source':args.source,'target_window_s':args.seconds,'clients':args.clients,
            'url':args.url,'client_processes':'one independent Chrome launch per client',
            'pc_role':'LAN viewer/measurement only; no PC port forwarding',
            'scope':'Animation-frame observation of decoded displayed.id changes; not physical display scanout or capture-to-display AoI. Browser observation counts are not server inference Hz.',
            'replay_note':'The current replay is 12.7s/30FPS. EOF and startup idle remain in whole-window FPS; stream FPS excludes idle and can be inflated by bursts.',
            'runs':[]}
    def save():output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    clients=[]
    with ExitStack() as stack:
        ssh=None
        if args.network_counters or not args.no_collect_events:
            keys=Path('/mnt/c/Users/operator/.ssh')
            ssh=stack.enter_context(connection('192.0.2.10','root',22,keys/'control-example',keys/'known_hosts'))
        engine=stack.enter_context(sync_playwright())
        try:
            for index in range(args.clients):
                browser=engine.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,
                    args=['--no-sandbox','--disable-dev-shm-usage','--disable-background-timer-throttling',
                          '--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows'])
                context=browser.new_context(viewport={'width':1024,'height':768},
                    http_credentials={'username':username,'password':credentials['password']})
                page=context.new_page()
                client={'browser':browser,'context':context,'page':page,'capture':False,'start':0,
                        'wire':new_wire(),'acks':[],'pending_acks':{},'errors':[]}
                clients.append(client);attach(page,client)
                page.goto(args.url,wait_until='domcontentloaded',timeout=30000)
                page.wait_for_function("typeof state!=='undefined'&&state!==null",timeout=20000)
                page.evaluate(PROBE)
                visible=page.evaluate('document.visibilityState')
                if visible!='visible':
                    raise RuntimeError('Client '+str(index)+' is not visible: '+visible)
            page=clients[0]['page'];context=clients[0]['context']
            for repeat in range(args.repeats+int(args.warmup)):
                if page.evaluate('!!active'):
                    raise RuntimeError('Another experiment is active; refusing to replace it')
                record={'repeat':repeat if args.warmup else repeat+1,'warmup':bool(args.warmup and repeat==0),
                        'arm':'C','source':args.source,'run_id':None,'clients':[]}
                result['runs'].append(record);save()
                owned=None
                if args.network_counters:
                    record['control_network_before']=network_counters(ssh)
                    record['control_counter_start_mono_s']=time.monotonic()
                page.select_option('#arm','C');page.select_option('#source',args.source)
                page.fill('#seconds',str(math.ceil(args.seconds+15)))
                for client in clients:
                    client.update(capture=True,start=time.monotonic(),wire=new_wire(),acks=[],pending_acks={},errors=[])
                    client['page'].evaluate('window.transportProbe.begin()')
                begin=time.monotonic()
                try:
                    with page.expect_response(lambda response:response.request.method=='POST' and urlsplit(response.url).path=='/api/start',timeout=15000) as response:
                        page.click('#start_button',timeout=10000)
                    response=response.value
                    if not response.ok:
                        raise RuntimeError('Start request rejected with HTTP '+str(response.status))
                    started=response.json();owned=started['run_id'];record['run_id']=owned;record['run']=started
                    for client in clients:
                        client['page'].evaluate('(id)=>window.transportProbe.bind(id)',owned)
                    end=begin+args.seconds
                    while time.monotonic()<end:
                        page.wait_for_timeout(min(250,max(1,(end-time.monotonic())*1000)))
                except Exception as error:
                    record['error']=type(error).__name__+': '+str(error)
                    raise
                finally:
                    for index,client in enumerate(clients):
                        client['capture']=False
                        try:
                            probe=client['page'].evaluate('window.transportProbe.finish()')
                            measurements=summarize_probe(probe,owned) if owned else probe
                            record['clients'].append({'index':index,'chrome_version':client['browser'].version,
                                **measurements,'wire':client['wire'],'ack_requests':len(client['acks']),
                                'ack_non_200':sum(r['status'] is not None and r['status']!=200 for r in client['acks']),
                                'ack_failed':sum(bool(r['failed']) for r in client['acks']),
                                'ack_pending':sum(r['status'] is None and not r['failed'] for r in client['acks']),
                                'ack_records':client['acks'],'errors':client['errors']})
                        except Exception as error:
                            record['clients'].append({'index':index,'collection_error':str(error)})
                    if owned:
                        try:record['stop']=stop_owned(page,context,base,owned)
                        except Exception as error:record['stop_error']=str(error)
                    if args.network_counters:
                        record['control_network_after']=network_counters(ssh)
                        record['control_counter_interval_s']=time.monotonic()-record['control_counter_start_mono_s']
                    save()
                print(json.dumps({'tag':args.tag,'repeat':record['repeat'],'warmup':record['warmup'],'run_id':owned,
                    'clients':[{k:c.get(k) for k in ['index','effective_draw_stream_fps','whole_window_fps',
                        'max_draw_gap_ms','p95_draw_gap_ms','same_frame_mismatches','max_boxes','browser_unique_observation_count',
                        'visibility_all_recorded_visible','ack_requests','ack_non_200','errors']} for c in record['clients']]},ensure_ascii=False),flush=True)
                page.wait_for_timeout(1500)
        finally:
            for client in clients:
                try:client['browser'].close()
                except Exception:pass
            if ssh and not args.no_collect_events:
                ids=[r['run_id'] for r in result['runs'] if r.get('run_id')]
                if ids:
                    try:
                        events=ROOT/f'results/{args.tag}-events.jsonl';collect_events(ssh,ids,events)
                        result['events_file']=str(events);result['events_bytes']=events.stat().st_size
                    except Exception as error:
                        result['events_collection_error']=str(error)
            save()


if __name__=='__main__':
    main()
