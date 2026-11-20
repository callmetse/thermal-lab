"""Exercise real LAN page buttons, image display, slash URLs and visible errors."""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    credentials = json.loads(Path('/mnt/c/Users/operator/.codex/secrets/abc20261102-console/login.json').read_text())
    base = credentials['url'].removesuffix('/lab')
    errors, requests, checks = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1100, 'height': 900},
                                      http_credentials={'username': credentials['username'], 'password': credentials['password']})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: requests.append({'path': urlparse(response.url).path, 'status': response.status})
                if urlparse(response.url).path in ['/api/start', '/api/stop'] else None)
        owned = None
        try:
            for suffix in ['/lab', '/lab/']:
                page.goto(base + suffix, wait_until='domcontentloaded')
                state = context.request.get(base + '/api/state').json()
                if state.get('active'):
                    raise RuntimeError('An experiment is active; refusing to interrupt it')
                page.wait_for_function("!document.querySelector('#start_button').disabled")
                page.select_option('#arm', 'C')
                page.select_option('#source', 'camera')
                page.fill('#seconds', '30')
                page.click('#start_button')
                page.wait_for_function("document.querySelector('#feedback').textContent.includes('已启动')", timeout=15000)
                state = context.request.get(base + '/api/state').json()
                owned = state['active']['run_id']
                page.wait_for_function("""() => {try {
                    const o=JSON.parse(document.querySelector('#obs').textContent),i=document.querySelector('#preview');
                    return o?.source==='camera'&&i.complete&&i.naturalWidth>0;
                } catch(error){return false;}}""", timeout=20000)
                if suffix == '/lab':
                    page.screenshot(path=str(ROOT / 'results/lan-console-live.png'))
                source = json.loads(page.locator('#obs').inner_text())
                page.click('#stop_button')
                page.wait_for_function("!document.querySelector('#start_button').disabled && document.querySelector('#stop_button').disabled", timeout=15000)
                assert context.request.get(base + '/api/state').json()['active'] is None
                checks.append({'page_path': suffix, 'run_id': owned, 'start_button': 'passed',
                               'physical_camera_frame': source['source_frame_id'],
                               'stop_button': 'passed', 'feedback_visible': True})
                owned = None
                page.wait_for_timeout(1500)
            # Deliberately fail one browser request, without changing the real server.
            page.route('**/api/start', lambda route: route.fulfill(status=503, content_type='application/json',
                       body=json.dumps({'error': '联调：模拟设备暂不可用'}, ensure_ascii=False)))
            page.click('#start_button')
            page.wait_for_function("document.querySelector('#error').textContent.includes('模拟设备暂不可用')")
            assert page.locator('#error').is_visible()
            assert context.request.get(base + '/api/state').json()['active'] is None
            checks.append({'error_feedback': 'passed', 'scope': 'browser-only simulated 503; no server fault injected'})
            page.unroute('**/api/start')
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("!document.querySelector('#start_button').disabled")
        finally:
            if owned:
                state = context.request.get(base + '/api/state').json()
                if (state.get('active') or {}).get('run_id') == owned:
                    context.request.post(base + '/api/stop', data={})
            browser.close()
    if errors:
        raise RuntimeError('LAN page script errors: ' + '; '.join(errors))
    output = {'url': base + '/lab', 'tests': checks, 'script_errors': errors,
              'button_http_results': requests, 'browser': 'real Chromium over Pi LAN IP',
              'local_pc_tunnel_used': False}
    (ROOT / 'results/lan-console-browser-checks.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
