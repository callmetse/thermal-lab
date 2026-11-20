"""Passive real-browser frame/observation counters for tagged experiments."""
import argparse
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser();p.add_argument('--seconds', type=int, default=360);p.add_argument('--output',default='perf-browser-runs.json');a = p.parse_args()
credentials=json.loads(Path('/mnt/c/Users/operator/.codex/secrets/abc20261102-console/login.json').read_text())
output=ROOT/'results'/a.output;stopfile=ROOT/'state/perf_observer_stop'
stopfile.unlink(missing_ok=True)
with sync_playwright() as engine:
    browser=engine.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1024,'height':768},http_credentials={'username':credentials['username'],'password':credentials['password']})
    page=context.new_page();errors=[];page.on('pageerror',lambda error: errors.append(str(error)));page.goto(credentials['url'])
    page.evaluate("""window.probes={};function pr(id){return window.probes[id]||(window.probes[id]={images:[],observations:{},max_boxes:0})}
    document.querySelector('#preview').addEventListener('load',()=>{const id=active?.run_id;if(!id)return;requestAnimationFrame(()=>requestAnimationFrame(()=>{if(active?.run_id===id){const r=pr(id);r.images.push(performance.now());r.max_boxes=Math.max(r.max_boxes,document.querySelectorAll('#overlay .box-target').length)}}))});
    new MutationObserver(()=>{try{const o=JSON.parse(document.querySelector('#obs').textContent);if(o&&!Object.hasOwn(pr(o.run_id).observations,o.source_frame_id))pr(o.run_id).observations[o.source_frame_id]=performance.now()}catch(error){}}).observe(document.querySelector('#obs'),{childList:true,subtree:true,characterData:true});""")
    end=time.monotonic()+a.seconds
    print('Passive tablet-size browser observer ready',flush=True)
    while time.monotonic()<end and not stopfile.exists():
        page.wait_for_timeout(1000)
        result={'runs':page.evaluate('window.probes'),'errors':errors,'viewport':[1024,768],'local_pc_tunnel':False}
        output.write_text(json.dumps(result,indent=2))
    browser.close()
print('Browser counters saved',flush=True)
