"""Exercise actual LAN controls, binary preview, matching, layout and HTTP fallback."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument('--seconds', type=int, default=25)
p.add_argument('--clients', type=int, default=1)
p.add_argument('--source', choices=['camera','replay'], default='camera')
p.add_argument('--tag', default='perf-final-live')
p.add_argument('--skip-fallback', action='store_true')
a = p.parse_args()
auth = json.loads(Path('/mnt/c/Users/operator/.codex/secrets/abc20261102-console/login.json').read_text())
errors, wire = [], {'binary_packets':0,'text_packets':0,'binary_bytes':0}

def socket_open(ws):
    if not ws.url.endswith('/live-video'):
        return
    def frame(payload):
        if isinstance(payload, bytes):
            wire['binary_packets'] += 1
            wire['binary_bytes'] += len(payload)
        else:
            wire['text_packets'] += 1
    ws.on('framereceived', frame)

with sync_playwright() as engine:
    browser = engine.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows'])
    context = browser.new_context(viewport={'width':1024,'height':768},http_credentials={'username':auth['username'],'password':auth['password']})
    pages = []
    for i in range(a.clients):
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('websocket', socket_open)
        page.goto(auth['url'])
        page.wait_for_function('state !== null && videoSocket?.readyState===1')
        page.evaluate("""window.actualDraws=[];window.mismatches=0;window.seenId='';
          function inspect(){if(displayed&&displayed.id!==window.seenId){window.seenId=displayed.id;
           const o=matchedObservation();window.actualDraws.push({id:displayed.id,t:performance.now(),boxes:document.querySelectorAll('#overlay .box-target').length});
           if(o&&keyOf(o)!==displayed.id)window.mismatches++;}requestAnimationFrame(inspect)}requestAnimationFrame(inspect);""")
        pages.append(page)
    page = pages[0]
    if page.evaluate('!!active'):
        raise RuntimeError('An experiment is already active; refusing to replace it')
    page.select_option('#arm','C')
    page.select_option('#source',a.source)
    page.fill('#seconds','90')
    try:
        page.click('#start_button')
        page.wait_for_function('active && displayed && matchedObservation()',timeout=45000)
        run = page.evaluate('active')
        for client in pages:client.evaluate('window.actualDraws=[]')
        page.wait_for_timeout(a.seconds*1000)
        metrics = []
        for client in pages:
            metrics.append(client.evaluate("""({draws:window.actualDraws,mismatches:window.mismatches,
             hud:document.querySelector('#fps_chip').textContent,obs_hud:document.querySelector('#obs_chip').textContent,
             match:document.querySelector('#match_status').textContent,thermal:document.querySelector('#track_detail').textContent,
             video_fresh:videoFresh(),llm:llmCurrent,viewport:[innerWidth,innerHeight]})"""))
        layouts = []
        for width,height,name in [(1024,768,'tablet-landscape'),(768,1024,'tablet-portrait'),(390,844,'phone')]:
            page.set_viewport_size({'width':width,'height':height})
            page.wait_for_timeout(500)
            layouts.append(page.evaluate("""({viewport:[innerWidth,innerHeight],overflow:document.documentElement.scrollWidth>innerWidth,
              image:[document.querySelector('#preview').naturalWidth,document.querySelector('#preview').naturalHeight],
              boxes:document.querySelectorAll('#overlay .box-target').length})"""))
            page.screenshot(path=str(ROOT/f'results/{a.tag}-{name}.png'),full_page=True)
        fallback = {'tested':False}
        if not a.skip_fallback:
            before = page.evaluate('displayed?.id')
            # Block only the native video WS; retain state WS and real HTTP fallback.
            page.evaluate('videoSocket.onclose=null;videoSocket.close();lastVideoPacketAt=0;videoConnectedAt=0;videoPending=null')
            page.wait_for_timeout(4500)
            fallback = page.evaluate('({tested:true,before:null,after:displayed?.id,video_fresh:videoFresh(),image_width:document.querySelector("#preview").naturalWidth})')
            fallback['before'] = before
            fallback['advanced'] = before != fallback['after'] and bool(fallback['after'])
    finally:
        if page.evaluate('!!active'):
            page.click('#stop_button')
            page.wait_for_function('!active')
            page.wait_for_timeout(500)
        stopped = page.evaluate('({image_removed:!document.querySelector("#preview").hasAttribute("src"),boxes:document.querySelectorAll("#overlay .box-target").length})')
    browser.close()

for r in metrics:
    t=[d['t'] for d in r['draws']]
    r['draw_fps']=(len(t)-1)*1000/(t[-1]-t[0]) if len(t)>1 else None
    r['max_draw_gap_ms']=max((b-a for a,b in zip(t,t[1:])),default=None)
result={'run':run,'source':a.source,'clients':a.clients,'physical_tablet_tested':False,'pc_role':'LAN viewer only; no local forwarding',
        'scope':'Decoded image/overlay sampled on animation frames, not display scanout or absolute capture-to-display latency',
        'errors':errors,'wire':wire,'metrics':metrics,'layouts':layouts,'http_fallback':fallback,'stopped':stopped}
(ROOT/f'results/{a.tag}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'run_id':run['run_id'],'source':a.source,'clients':a.clients,'draw_fps':[r['draw_fps'] for r in metrics],
                  'max_boxes':[max((d['boxes'] for d in r['draws']),default=0) for r in metrics], 'wire':wire,'errors':errors,
                  'layouts':layouts,'http_fallback':fallback,'stopped':stopped},ensure_ascii=False),flush=True)
