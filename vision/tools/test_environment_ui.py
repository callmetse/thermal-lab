"""Authenticated browser integration test. Credentials are read privately, never printed."""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'state/e2e_browser'))
from playwright.sync_api import sync_playwright


def main():
    out=Path('D:/thermal-lab-archive/05_实测结果/e2e_20261116_1605')
    secret=json.loads(Path('/etc/thermal-lab/private/operator.json').read_text(encoding='utf-8-sig'))
    base='http://192.0.2.11:28880'
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True,args=['--no-proxy-server'])
        ctx=browser.new_context(viewport={'width':1440,'height':1200},http_credentials={
            'username':secret.get('user') or secret.get('username'),'password':secret['password']})
        page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/lab',wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('env_temperature').textContent.includes('°C')",timeout=12000)
        page.fill('#collection_location','联调测试地点（测试标签）')
        page.fill('#collection_name','环境批次关联实测_20261116')
        page.check('#unlimited');page.select_option('#arm','C');page.select_option('#source','camera')
        page.click('#start_button')
        page.wait_for_function("document.getElementById('stop_button').disabled===false",timeout=12000)
        active=ctx.request.get(base+'/api/state').json()['active']
        batch_id=active['collection_batch_id'];assert batch_id==active['run_id']
        try:
            page.wait_for_timeout(15000)
            live=ctx.request.get(base+'/api/raw-state').json()
            observation=live['observation']
            assert observation['collection_batch_id']==batch_id
            assert observation['collection_location']=='联调测试地点（测试标签）'
            archived=ctx.request.get(base+'/api/collection-batches/'+batch_id+'/environment?limit=1000').json()
            assert archived['count']>0
            assert {r['sensor_id'] for r in archived['rows']}=={'sht4x','scd41'}
            page.click('#refresh_batches');page.wait_for_timeout(500)
            page.screenshot(path=str(out/'environment_batch_live.png'),full_page=True)
            ui={key:page.locator('#'+key).inner_text() for key in ['env_temperature','env_humidity','env_co2','env_crosscheck','env_device','batch_metadata']}
        finally:
            current=ctx.request.get(base+'/api/state').json().get('active')
            if current and current['run_id']==active['run_id']:
                page.click('#stop_button')
                page.wait_for_function("document.getElementById('start_button').disabled===false",timeout=12000)
        for _ in range(30):
            batch=ctx.request.get(base+'/api/collection-batches/'+batch_id).json()
            if batch['status']=='completed':break
            page.wait_for_timeout(300)
        assert batch['status']=='completed',batch
        assert batch['capture_error'] is None,batch
        response=ctx.request.get(base+'/api/collection-batches/'+batch_id+'/environment?limit=10000')
        data=response.json();assert data['batch']['location']=='联调测试地点（测试标签）'
        csv_response=ctx.request.get(base+'/api/collection-batches/'+batch_id+'/environment?format=csv&all=1')
        assert csv_response.status==200 and 'sensor_id' in csv_response.text()
        assert ctx.request.get(base+'/api/collection-batches/nonexistent').status==404
        assert ctx.request.get(base+'/api/collection-batches/'+batch_id+'/environment?limit=10001').status==400
        assert not errors,errors
        result={'batch_id':batch_id,'run_id':active['run_id'],'location':batch['location'],
                'name':batch['name'],'environment_rows':data['count'],'sensor_ids':sorted({r['sensor_id'] for r in data['rows']}),
                'status':batch['status'],'capture_error':batch['capture_error'],'browser_errors':errors,
                'ui':ui,'visual_observation_bound_to_batch':True,
                'json_http_status':response.status,'csv_http_status':csv_response.status,
                'missing_batch_status':404,'invalid_limit_status':400}
        (out/'environment_batch_test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'environment_batch_data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'environment_batch_data.csv').write_bytes(csv_response.body())
        print(json.dumps(result,ensure_ascii=False))
        browser.close()


if __name__=='__main__':main()
