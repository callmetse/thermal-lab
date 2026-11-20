import sys,json
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'state/e2e_browser'));sys.path.insert(0,str(ROOT/'tools'))
from e2e_collaboration import remote
from playwright.sync_api import sync_playwright
out=Path('D:/thermal-lab-archive/05_实测结果/environment_interval_20261116');out.mkdir(parents=True,exist_ok=True)
base='http://192.0.2.11:28880'
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True,args=['--no-proxy-server'])
 ctx=browser.new_context(viewport={'width':1440,'height':1100},http_credentials={'username':os.environ.get('ABC_UI_USER','lab'),'password':os.environ.get('ABC_UI_PASSWORD','')})
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto(base+'/data');page.wait_for_function("document.getElementById('env_setting_status').textContent.includes('当前每')")
 before=ctx.request.get(base+'/api/environment/longterm/settings').json()
 try:
  page.fill('#env_interval','5');page.click('#env_apply');page.wait_for_function("document.getElementById('env_setting_status').textContent.includes('当前每 5 秒')")
  remote('pi',"import subprocess;subprocess.run(['/home/pi/abc20261102/.venv/bin/supervisorctl','-c','/home/pi/env_sensor_v1/supervisord.ini','restart','env-gateway'],check=True)")
  assert ctx.request.get(base+'/api/environment/longterm/settings').json()['interval_s']==5
  page.wait_for_timeout(18000)
  data=ctx.request.get(base+'/api/environment/longterm/history').json()
  five=[r for r in data['rows'] if r['interval_s']==5 and not r['partial']]
  assert len(five)>=2,data
  assert all(r['end_ms']-r['start_ms']==5000 for r in five)
  assert any('lab/sht4x' in r['sensors'] and 'lab/scd41' in r['sensors'] for r in five)
  assert page.locator('#env_charts svg').count()==3
  assert page.locator('#env_charts circle').count()>0
  page.screenshot(path=str(out/'环境曲线.png'),full_page=True)
  assert ctx.request.post(base+'/api/environment/longterm/settings',data={'interval_s':6}).status==400
  latest=data['rows'][-1]['id'];assert all(row['id']>latest for row in ctx.request.get(base+'/api/environment/longterm/history?after_id='+str(latest)).json()['rows'])
  assert ctx.request.get(base+'/api/state').json()['active'] is None
  assert not errors,errors
  (out/'history.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
 finally:
  response=ctx.request.post(base+'/api/environment/longterm/settings',data={'interval_s':before['interval_s']});assert response.status==200
 result={'initial_interval_s':before['interval_s'],'test_interval_s':5,'full_five_second_windows':len(five),'restart_preserved':True,'charts':3,'browser_errors':errors,'vision_idle':True,'restored_interval_s':response.json()['interval_s']}
 (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
 browser.close()
