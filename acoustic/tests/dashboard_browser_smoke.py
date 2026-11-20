"""Browser integration checks using synthetic REST and WebSocket responses, no devices."""
import base64
import json
import struct
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/dashboard_20261104'
OUT.mkdir(exist_ok=True)

def main():
    mm={'active':None,'session_id':'old','status':'finished','events':[],'stats':{},'worker_age_s':40000,'mqtt_connected':True,'worker':{},'newest_result_age_s':40000}
    visual={'active':None,'observation':None}
    calls=[]; errors=[]; sockets=[]
    def handle(route):
        path=route.request.url.split('http://mm.test')[-1]
        body=route.request.post_data_json if route.request.method=='POST' else None
        if body is not None:calls.append((path,body))
        result={}
        if path=='/mm':return route.fulfill(content_type='text/html',body=(ROOT/'page.html').read_text())
        if path=='/mm/api/state':result=mm
        elif path=='/api/state':result=visual
        elif path=='/api/start':
            visual['active']={**body,'run_id':'new-run'};result=visual['active']
        elif path=='/mm/api/start':
            mm.update(active='new-audio',session_id='new-audio',status='recording',events=[],stats={})
            result={'session_id':'new-audio'}
        elif path=='/mm/api/stop':mm['active']=None;result={'status':'stopping'}
        elif path=='/mm/api/confirm':
            event={'kind':'confirmed_feedback','event_id':'confirmed-1','session_id':mm['session_id'],'evidence_refs':[body['event_id']],'asr_text':'我有点冷','transcript_override':body['transcript'], 'sensation':body['sensation'],'preference':body['preference']}
            mm['events'].insert(0,event);result=event
        else:return route.fulfill(status=404,body='missing mock route')
        route.fulfill(content_type='application/json',body=json.dumps(result))
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1440,'height':1080})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('http://mm.test/**',handle)
        page.route_web_socket('ws://mm.test/live-video',lambda socket:sockets.append(socket))
        page.goto('http://mm.test/mm')
        page.wait_for_function("document.getElementById('noticeText').textContent.includes('未就绪')")
        assert page.locator('#start').is_disabled()
        assert not calls, 'Page load must not start recording'
        mm['worker_age_s']=1
        page.wait_for_function("!document.getElementById('start').disabled")
        for mode in ['A','B','C']:
            page.locator(f'[data-mode="{mode}"]').click()
            page.locator('#start').click()
            page.wait_for_function("!!mm?.active")
            assert [b for path,b in calls if path=='/api/start'][-1]['arm']==mode
            assert [b for path,b in calls if path=='/mm/api/start'][-1]['parent_run_id']=='new-run'
            page.locator('#stop').click()
            page.wait_for_function("!mm?.active")
            assert not any(path=='/api/stop' for path,b in calls),'Must not stop another visual run'
            visual['active']=None
            page.wait_for_function("!visual?.active")
        # Existing visual session must be reused, not replaced.
        visual['active']={'run_id':'existing-B','arm':'B','source':'camera'}
        page.wait_for_function("visual?.active?.run_id==='existing-B'")
        prior=len([x for x in calls if x[0]=='/api/start'])
        page.locator('#start').click()
        page.wait_for_function("!!mm?.active")
        assert len([x for x in calls if x[0]=='/api/start'])==prior
        assert [b for path,b in calls if path=='/mm/api/start'][-1]['parent_run_id']=='existing-B'
        tracks=[{'track_id':i,'bbox':[20+i*70,30,80+i*70,300],'posture':{'state':'standing'},'action':{'value':'unknown'},'clothing':[{'category':'pants'}],'clo':None,'met':None} for i in range(6)]
        obs={'run_id':'existing-B','source_frame_id':1,'encoded_size':[640,360],'jpeg_sha256':'abc','tracks':tracks}
        meta={**obs,'observation':obs,'preview_id':'existing-B:1','pi_capture_to_preview_ms':123}
        jpeg=base64.b64decode(page.evaluate("(()=>{const c=document.createElement('canvas');c.width=640;c.height=360;const x=c.getContext('2d');x.fillStyle='#dfe9df';x.fillRect(0,0,640,360);return c.toDataURL('image/jpeg').split(',')[1]})()"))
        def send(m):
            header=json.dumps(m).encode();sockets[-1].send(struct.pack('>I',len(header))+header+jpeg)
        page.wait_for_timeout(100)
        send({**meta,'run_id':'wrong-run','preview_id':'wrong-run:1'})
        page.wait_for_timeout(80)
        assert page.locator('#video').is_hidden()
        send(meta)
        page.wait_for_function("document.querySelectorAll('#personTabs button').length===6")
        assert page.locator('#videoOverlay rect').count()==6
        page.locator('#personTabs button').last.click()
        assert page.locator('#personTitle').inner_text()=='人物 5'
        # Hash mismatch must remove overlays, even if frame id matches.
        obs2={**obs,'source_frame_id':2}
        send({**meta,'source_frame_id':2,'preview_id':'existing-B:2','jpeg_sha256':'different','observation':obs2})
        page.wait_for_function("document.getElementById('frameLabel').textContent==='帧 2'")
        assert page.locator('#videoOverlay rect').count()==0
        send({**meta,'source_frame_id':3,'preview_id':'existing-B:3','observation':{**obs,'source_frame_id':3}})
        page.wait_for_function("document.querySelectorAll('#personTabs button').length===6")
        event={'event_id':'speech-1','session_id':mm['session_id'],'kind':'transcript','text':'我有点冷','candidate':{'sensation':'cold','preference':'warmer'},'pi_result_received_mono_ns':1000000000,'infer_ms':40}
        mm['events']=[event];mm['newest_result_age_s']=0.1
        page.wait_for_function("document.getElementById('transcript').value==='我有点冷'")
        assert page.locator('.choice.selected').count()==0, 'Candidate must not auto-select human label'
        page.locator('#transcript').fill('我现在有一点冷')
        page.wait_for_timeout(1200)
        assert page.locator('#transcript').input_value()=='我现在有一点冷','Polling overwrote edit'
        page.locator('[data-sensation="cold"]').click()
        page.locator('[data-preference="warmer"]').click()
        page.locator('#confirm').click()
        page.wait_for_function("!document.getElementById('confirmation').hidden")
        assert [b for path,b in calls if path=='/mm/api/confirm'][-1]=={'event_id':'speech-1','transcript':'我现在有一点冷','sensation':'cold','preference':'warmer'}
        assert page.locator('#confirm').is_disabled()
        page.locator('.tab[data-tab="records"]').click()
        with page.expect_download() as dl:page.locator('#export').click()
        exported=json.loads(Path(dl.value.path()).read_text())
        assert exported['events'][1]['text']=='我有点冷'
        page.locator('.tab[data-tab="workbench"]').click()
        # Finished results move to records, not live speech. Explicit history remains reviewable.
        mm['active']=None
        page.wait_for_function("document.getElementById('transcript').value===''")
        page.locator('.tab[data-tab="records"]').click()
        page.get_by_role('button',name='查看已确认反馈').click()
        assert page.locator('#transcript').input_value()=='我现在有一点冷'
        # Reconnect/readiness and stale-frame behavior.
        page.wait_for_function("document.getElementById('video').hidden",timeout=6000)
        assert page.locator('#personTabs button').count()==0
        mm['worker_age_s']=20
        page.wait_for_function("document.getElementById('start').disabled")
        mm['worker_age_s']=1;visual['active']=None
        page.wait_for_function("!document.getElementById('start').disabled && !visual.active")
        page.locator('#captureMode').select_option('audio')
        prior=len([x for x in calls if x[0]=='/api/start'])
        page.locator('#start').click();page.wait_for_function("!!mm?.active")
        assert len([x for x in calls if x[0]=='/api/start'])==prior
        assert [b for path,b in calls if path=='/mm/api/start'][-1]['parent_run_id'] is None
        sizes=[]
        for name,w,h in [('desktop',1440,1080),('tablet',1024,768),('portrait',820,1180),('phone',390,844)]:
            page.set_viewport_size({'width':w,'height':h})
            assert page.evaluate('document.documentElement.scrollWidth')<=w
            page.screenshot(path=str(OUT/f'mock-{name}.png'),full_page=True)
            sizes.append([w,h])
        assert not errors,errors
        browser.close()
    report={'fixture':'synthetic only; no live camera or microphone','passed':['offline gating','A/B/C start payload','existing visual session reuse','audio-only','stop audio only','six people','wrong-run rejection','hash mismatch rejection','stale image clearing','manual labels','edit preserved across polling','confirmation payload','history separated','JSON export','four viewports'], 'javascript_errors':errors,'viewports':sizes}
    (OUT/'browser-checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
