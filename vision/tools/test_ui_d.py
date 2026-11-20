import os
import sys,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'state/e2e_browser'))
from playwright.sync_api import sync_playwright
OUT=Path('D:/thermal-lab-archive/05_实测结果/ui_d_20261116');OUT.mkdir(parents=True,exist_ok=True)
base='http://192.0.2.11:28880'
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True,args=['--no-proxy-server'])
 ctx=browser.new_context(viewport={'width':1440,'height':1100},http_credentials={'username':'lab','password':os.environ['THERMAL_LAB_PASSWORD']})
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 assert ctx.request.get(base+'/api/state').json()['active'] is None
 page.goto(base+'/lab');page.wait_for_timeout(1500)
 page.select_option('#arm','D');page.set_input_files('#video_file',str(ROOT/'state/ui-d-20261116/test.mp4'));page.click('#upload_button')
 page.wait_for_function("document.getElementById('upload_status').textContent.includes('上传完成')",timeout=30000)
 page.fill('#collection_location','D上传通道测试地点');page.fill('#collection_name','上传视频验收')
 page.on('response',lambda r:print('START',r.status,r.text(),flush=True) if r.url.endswith('/api/start') else None);page.click('#start_button');page.wait_for_function("!document.getElementById('stop_button').disabled",timeout=12000)
 run=ctx.request.get(base+'/api/state').json()['active'];observations=[]
 try:
  for i in range(30):
   s=ctx.request.get(base+'/api/raw-state').json()
   print('probe',i, bool(s.get('active')), bool(s.get('observation')),ctx.request.get(base+'/api/state').json().get('run_error'),flush=True)
   if s.get('observation'):observations.append(s['observation'])
   if i==10:page.screenshot(path=str(OUT/'D通道实测.png'),full_page=True)
   if not s.get('active'):break
   page.wait_for_timeout(1000)
  assert observations,str(errors)+page.locator('#error').inner_text()+page.locator('#feedback').inner_text()
  assert ctx.request.get(base+'/api/state').json()['active'] is None,'did not auto stop'
 finally:
  current=ctx.request.get(base+'/api/state').json().get('active')
  if current and current['run_id']==run['run_id']:ctx.request.post(base+'/api/stop',data={})
 page.goto(base+'/data');page.wait_for_timeout(1000);page.fill('#search','D上传通道测试地点');page.get_by_role('button',name='查看数据',exact=True).first.click();page.wait_for_timeout(700)
 assert page.locator('#rows tr').count()>0
 csv=ctx.request.get(base+'/api/collection-batches/'+run['run_id']+'/environment?format=csv&all=1');assert csv.status==200
 (OUT/'上传批次环境.csv').write_bytes(csv.body())
 page.screenshot(path=str(OUT/'数据查询页.png'),full_page=True)
 invalid=ctx.request.post(base+'/api/video-uploads',data=b'not video');assert invalid.status==400
 assert not errors,errors
 report={'run':run,'observation_snapshots':len(observations),'first_observation':observations[0],'last_observation':observations[-1],'auto_stopped':True,'csv_status':csv.status,'invalid_video_status':invalid.status,'browser_errors':errors}
 (OUT/'browser_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'observations':len(observations),'auto_stopped':True,'errors':errors}))
 browser.close()
