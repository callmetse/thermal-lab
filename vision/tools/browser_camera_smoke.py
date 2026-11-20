"""Real Chromium rendering via a viewing-only tunnel; never fabricates display ACKs."""
import argparse
import json
from pathlib import Path
import socket
import time

from playwright.sync_api import sync_playwright
from ssh_access import connection

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--tag', choices=['live-browser', 'live-browser-A', 'live-browser-B', 'live-browser-C'], default='live-browser')
    parser.add_argument('--save-diagnostic-frame', action='store_true')
    args = parser.parse_args()
    ssh = Path('/mnt/c/Users/operator/.ssh')
    with connection('192.0.2.10', 'root', 22, ssh / 'control-example',
                    ssh / 'known_hosts', local_forward=(38880, 28880)) as run:
        result = run('cat /etc/abc20261102/ui-login.json', timeout=15)
        if result.returncode:
            raise RuntimeError('Cannot read the independent console credential')
        auth = json.loads(result.stdout)
        for attempt in range(100):
            try:
                with socket.create_connection(('127.0.0.1', 38880), timeout=.2):
                    break
            except OSError:
                if attempt == 99:
                    raise RuntimeError('Viewing-only tunnel did not become ready')
                time.sleep(.1)
        errors, snapshots = [], []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                        args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = browser.new_context(viewport={'width': 390, 'height': 900},
                                          http_credentials={'username': auth['user'], 'password': auth['password']})
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:38880/lab', wait_until='domcontentloaded')
            page.wait_for_function("""() => {
                try {
                const img=document.querySelector('#preview');
                const obs=JSON.parse(document.querySelector('#obs').textContent);
                return img.complete && img.naturalWidth>0 && obs && obs.source==='camera';
                } catch(error) {return false;}
            }""", timeout=45000)
            page.wait_for_function("() => {try {return JSON.parse(document.querySelector('#llm').textContent)?.model==='qwen2.5:7b';} catch(error) {return false;}}", timeout=45000)
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                snapshots.append(page.evaluate("""() => ({obs:JSON.parse(document.querySelector('#obs').textContent),
                    llm:JSON.parse(document.querySelector('#llm').textContent),
                    preview_width:document.querySelector('#preview').naturalWidth,
                    status:document.querySelector('#status').textContent})"""))
                page.wait_for_timeout(1000)
            frame = context.request.get('http://127.0.0.1:38880/api/preview')
            if frame.status == 200 and args.save_diagnostic_frame:
                (ROOT / 'results/live-diagnostic-input.jpg').write_bytes(frame.body())
            page.screenshot(path=str(ROOT / ('results/' + args.tag + '-mobile-viewport.png')))
            page.locator('#llm').scroll_into_view_if_needed()
            page.screenshot(path=str(ROOT / ('results/' + args.tag + '-llm.png')))
            browser.close()
        output = {'browser': 'real local Chromium', 'viewport': [390, 900],
                  'physical_phone_tested': False, 'pc_role': 'viewer only; not camera/broker/inference/relay',
                  'errors': errors, 'snapshots': snapshots,
                  'ack_source': 'unmodified page image.onload and requestAnimationFrame callbacks'}
        (ROOT / ('results/' + args.tag + '-smoke.json')).write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(json.dumps({'snapshots': len(snapshots), 'errors': errors,
                          'runs': list({s['obs']['run_id'] for s in snapshots if s['obs']}),
                          'max_people': max((len(s['obs']['tracks']) for s in snapshots if s['obs']), default=0)}))


if __name__ == '__main__':
    main()
