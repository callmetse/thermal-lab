"""Local protocol fixture only: no camera, credentials, or cloud connections.

Run with state/browser-venv/bin/python tools/browser_transport_check.py.
Page Visibility/offline events are simulated; this does not test physical devices.
"""
import json
from pathlib import Path
import struct
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
page_html = (ROOT / 'deploy/nodered/page.html').read_text()
jpeg = (ROOT / 'results/live-diagnostic-input.jpg').read_bytes()


def observation(frame):
    return {'project':'abc20261102','run_id':'fixture','arm':'C','source':'replay',
            'source_frame_id':frame,'jpeg_sha256':f'input-{frame}','encoded_size':[640,480],
            'tracks':[{'track_id':n,'bbox':[30+n*110,30,120+n*110,420],
                       'posture':{'state':'standing'},'action':{'value':'unknown'},
                       'clothing':[],'clo':None,'met':None} for n in range(2)]}


def packet(frame, mismatch=False):
    obs = observation(frame-1 if mismatch else frame)
    meta = {'project':'abc20261102','run_id':'fixture','source_frame_id':frame,
            'preview_id':f'fixture:{frame}','jpeg_sha256':f'input-{frame}',
            'encoded_size':[640,480],'preview_size':[640,480],'observation':obs}
    header = json.dumps(meta).encode()
    return struct.pack('!I',len(header)) + header + jpeg


fixture = {'active':None,'observation':None,'llm':None,'preview_id':None,
           'observation_age_s':0,'llm_age_s':0}
requests, acks, sockets, errors = [], [], [], []


def http(route):
    request = route.request
    path = urlparse(request.url).path
    requests.append(path)
    if path == '/lab':
        route.fulfill(status=200,content_type='text/html',body=page_html)
    elif path == '/api/state':
        route.fulfill(status=200,json=fixture)
    elif path == '/api/start':
        fixture['active'] = {**request.post_data_json,'run_id':'fixture'}
        route.fulfill(status=200,json=fixture['active'])
    elif path == '/api/stop':
        fixture.update(active=None,observation=None,llm=None,llm_id=None,preview_id=None)
        route.fulfill(status=200,json={'status':'stopping'})
    elif path == '/api/ack':
        acks.append(request.post_data_json)
        route.fulfill(status=200,json={'ok':True})
    elif path == '/api/preview-frame':
        frame = int(parse_qs(urlparse(request.url).query)['v'][0].split(':')[-1])
        route.fulfill(status=200,content_type='application/octet-stream',body=packet(frame),
                      headers={'X-Preview-Id':f'fixture:{frame}'})
    else:
        route.fulfill(status=404,json={'error':'fixture has no such route'})


def socket_route(route):
    sockets.append(route)


def newest_socket(suffix):
    return next(s for s in reversed(sockets) if s.url.endswith(suffix))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,
                                       args=['--no-sandbox','--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width':1024,'height':768})
    page.route('http://fixture.local/**',http)
    page.route_web_socket('ws://fixture.local/**',socket_route)
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto('http://fixture.local/lab')
    page.wait_for_function('state!==null&&socket?.readyState===1&&videoSocket?.readyState===1')
    assert len(sockets)==2, 'only one socket per stream after initial pageshow'
    page.click('#start_button')
    page.wait_for_function('active?.run_id==="fixture"')

    # A binary packet alone updates the image, same-frame boxes and debug observation.
    newest_socket('/live-video').send(packet(10))
    page.wait_for_function('displayed?.id==="fixture:10"&&document.querySelectorAll("#overlay .box-target").length===2')
    assert page.evaluate('JSON.parse(document.querySelector("#obs").textContent).source_frame_id')==10
    page.wait_for_timeout(100)
    for frame in range(11,26):
        newest_socket('/live-video').send(packet(frame))
        page.wait_for_timeout(80)
    page.wait_for_timeout(100)
    preview_acks = [a for a in acks if a['kind']=='preview']
    assert 1<=len(preview_acks)<=2, 'preview ACKs are sampled at no more than one per second'
    assert all(a['sampled'] is True and a['sample_max_hz']==1 for a in preview_acks)

    # Repeated state snapshots do not repeatedly rewrite the same debug JSON nodes.
    fixture['observation'] = observation(25)
    fixture['observation_delivery_hz'] = 23.5
    page.evaluate("window.debugMutations=0;new MutationObserver(()=>window.debugMutations++).observe(document.querySelector('#obs'),{childList:true,subtree:true,characterData:true})")
    newest_socket('/live').send(json.dumps(fixture))
    page.wait_for_timeout(50)
    assert '23.5' in page.locator('#obs_chip').inner_text(), 'server delivery rate must not be replaced by client sample rate'
    before = page.evaluate('window.debugMutations')
    for _ in range(5):newest_socket('/live').send(json.dumps(fixture))
    page.wait_for_timeout(100)
    assert page.evaluate('window.debugMutations')==before

    newest_socket('/live-video').send(packet(30,mismatch=True))
    page.wait_for_function('displayed?.id==="fixture:30"')
    assert page.locator('#overlay .box-target').count()==0, 'wrong-frame metadata cannot draw boxes'
    newest_socket('/live-video').send(packet(31))
    page.wait_for_function('displayed?.id==="fixture:31"&&matchedObservation()?.source_frame_id===31')

    # Simulate visibilitychange. Hidden pages must close BOTH streams and stop GETs/ACKs.
    page.evaluate("window.fixtureHidden=false;Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.fixtureHidden});window.oldVideoMessage=videoSocket.onmessage;window.oldStateMessage=socket.onmessage;window.fixtureHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function('socket===null&&videoSocket===null')
    before_requests = len(requests)
    page.evaluate("ack('preview','fixture:31')")
    page.wait_for_timeout(1400)
    assert len(requests)==before_requests, 'no polling, fallback or ACK while hidden'
    old_count = len(sockets)
    page.evaluate("window.fixtureHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function('socket?.readyState===1&&videoSocket?.readyState===1')
    assert len(sockets)==old_count+2
    page.evaluate("window.oldStateMessage({data:JSON.stringify({active:{run_id:'stale',arm:'A'}})});window.oldVideoMessage({data:new ArrayBuffer(10)})")
    assert page.evaluate('active.run_id')=='fixture', 'late events from old sockets are ignored'

    # Normal state-based HTTP fallback still obtains the requested atomic packet.
    page.evaluate('videoSocket.onclose=null;videoSocket.close();videoSocket=null;lastVideoPacketAt=videoConnectedAt=0')
    fixture['preview_id']='fixture:80'
    newest_socket('/live').send(json.dumps(fixture))
    page.wait_for_function('displayed?.id==="fixture:80"')
    assert page.locator('#overlay .box-target').count()==2

    page.evaluate("window.fixtureOnline=false;Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>window.fixtureOnline});window.dispatchEvent(new Event('offline'))")
    page.wait_for_function('socket===null&&videoSocket===null')
    page.wait_for_timeout(100)
    before_requests=len(requests)
    page.wait_for_timeout(1100)
    assert len(requests)==before_requests
    page.evaluate("window.fixtureOnline=true;window.dispatchEvent(new Event('online'))")
    page.wait_for_function('socket?.readyState===1&&videoSocket?.readyState===1')

    # Each new displayed LLM result still gets an unsampled ACK.
    for frame in [81,82,83]:
        fixture.update(llm_id=f'fixture:{frame}',llm={'run_id':'fixture','source_frame_id':frame,'status':'generated','draft':{'summary':'fixture','uncertainty':'unknown'}})
        newest_socket('/live').send(json.dumps(fixture))
        page.wait_for_timeout(100)
    assert len([a for a in acks if a['kind']=='llm'])==3
    assert all(a['sampled'] is False for a in acks if a['kind']=='llm')
    page.click('#stop_button')
    page.wait_for_function('!active&&!document.querySelector("#preview").hasAttribute("src")')
    assert page.locator('#overlay .box-target').count()==0
    assert not errors, errors
    metrics=page.evaluate('ackMetrics')
    first_client=page.evaluate('ackClientId')
    assert first_client and all(a.get('client_id')==first_client for a in acks)
    second=browser.new_page()
    second.route('http://fixture.local/**',http)
    second.route_web_socket('ws://fixture.local/**',socket_route)
    second.goto('http://fixture.local/lab')
    second.bring_to_front()
    second.wait_for_function('state!==null&&socket?.readyState===1&&videoSocket?.readyState===1')
    second_client=second.evaluate('ackClientId')
    assert second_client and first_client!=second_client, 'two pages need distinct acknowledgement identities'
    second.click('#start_button')
    second.wait_for_function('active?.run_id==="fixture"')
    second.evaluate("ack('preview','fixture:999')")
    second.wait_for_timeout(100)
    assert any(a.get('client_id')==second_client for a in acks)
    before_local=len(acks)
    second.evaluate("state.preview_transport='pi-local';lastPreviewAck=-Infinity;ack('preview','fixture:1000')")
    second.wait_for_timeout(100)
    assert len(acks)==before_local, 'local preview must not fabricate a cloud timing ACK'
    second.click('#stop_button')
    second.wait_for_function('!active')
    assert not errors, errors
    browser.close()

result={'scope':'Local mocked HTTP/WS, visibility and offline fixture; no field accuracy or throughput claim',
        'checks':'passed','preview_acks_during_1_4_s':len(preview_acks),
        'llm_acks':len([a for a in acks if a['kind']=='llm']),
        'ack_metrics':metrics,'javascript_errors':errors,'distinct_page_client_ids':first_client!=second_client,
        'checks_verified':['binary-only image/observation/boxes','same-frame rejection','sampled serial ACK','per-page ACK identity',
                           'debug DOM deduplication','server delivery Hz independent of state push rate','hidden/offline no traffic','visible/online reconnect',
                           'old socket callbacks ignored','atomic HTTP fallback','start/stop controls','local preview has no invalid cloud ACK']}
(ROOT/'results/browser-transport-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
