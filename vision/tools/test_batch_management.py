import os
import sys,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'state/e2e_browser'))
from e2e_collaboration import remote
from playwright.sync_api import sync_playwright
out=Path('D:/thermal-lab-archive/05_实测结果/batch_management_20261116');out.mkdir(parents=True,exist_ok=True)
ids=['management_fixture_'+str(int(time.time()))+'_'+str(i) for i in range(2)]
remote('pi',f"""import sqlite3,json,time
c=sqlite3.connect('/home/pi/abc20261102/data/collection_batches.sqlite3')
for bid in {ids!r}:
 c.execute("INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,ended_ms,status,run_json) VALUES(?,?,?,?,?,?,?,?,?)",(bid,bid,'管理功能验收专用','测试记录（非实测）','lab',int(time.time()*1000),int(time.time()*1000),'completed',json.dumps({{'run_id':bid,'arm':'C'}})))
c.commit();c.close()
""")
base='http://192.0.2.11:28880'
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True,args=['--no-proxy-server'])
 ctx=browser.new_context(viewport={'width':1440,'height':1000},http_credentials={'username':'lab','password':os.environ['THERMAL_LAB_PASSWORD']})
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
 page.goto(base+'/data');page.fill('#search','管理功能验收专用');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.click('#select_visible');page.locator('[data-action="favorite"]').click();page.wait_for_function("document.getElementById('operation').textContent.includes('已收藏 2')")
 page.select_option('#view','favorites');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.click('#select_visible');page.locator('[data-action="delete"]').click();page.wait_for_function("document.getElementById('operation').textContent.includes('已移入回收站 2')");page.wait_for_function("document.querySelectorAll('.batch-select').length===0")
 page.select_option('#view','trash');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.click('#select_visible');page.locator('[data-action="restore"]').click();page.wait_for_function("document.getElementById('operation').textContent.includes('已恢复 2')")
 page.select_option('#view','favorites');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.locator('[data-batch-id="'+ids[0]+'"] button').filter(has_text='取消收藏').click();page.wait_for_function("document.querySelectorAll('.batch-select').length===1")
 page.select_option('#view','all');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.locator('[data-batch-id="'+ids[0]+'"] button').filter(has_text='删除').click();page.wait_for_function("document.querySelectorAll('.batch-select').length===1")
 page.select_option('#view','trash');page.wait_for_function("document.querySelectorAll('.batch-select').length===1")
 page.get_by_role('button',name='恢复',exact=True).click();page.wait_for_function("document.querySelectorAll('.batch-select').length===0")
 page.select_option('#view','all');page.wait_for_function("document.querySelectorAll('.batch-select').length===2")
 page.screenshot(path=str(out/'管理页面.png'),full_page=True)
 response=ctx.request.post(base+'/api/collection-batches/manage',headers={'Origin':'https://foreign.example'},data={'action':'delete','batch_ids':ids});assert response.status==403
 assert not errors,errors
 # Leave only the synthetic fixtures in trash, with no favorites.
 for action in ['unfavorite','delete']:
  response=ctx.request.post(base+'/api/collection-batches/manage',data={'action':action,'batch_ids':ids});assert response.status==200
 result={'fixture_ids':ids,'bulk_favorite_delete_restore':True,'single_unfavorite_delete_restore':True,'foreign_origin_status':403,'browser_errors':errors,'final_fixture_state':'trash','real_experiments_modified':False}
 (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
 browser.close()
