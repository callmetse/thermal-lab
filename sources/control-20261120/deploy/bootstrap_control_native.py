"""Install ONLY the new project services; does not import old Node-RED flows."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
if ROOT != Path('/srv/abc20261102'):
    raise SystemExit('Run from the independently staged /srv/abc20261102 only')
for name in ['abc20261102-broker', 'abc20261102-console']:
    if Path('/etc/systemd/system/' + name + '.service').exists():
        raise SystemExit('New service already exists; inspect before changing it: ' + name)

(ROOT / 'package.json').write_text(json.dumps({
    'private': True, 'name': 'abc20261102-console', 'version': '0.1.0',
    'dependencies': {'node-red': '4.1.1', 'bcryptjs': '2.4.3'}}, indent=2) + '\n')
subprocess.run(['npm', 'install', '--omit=dev', '--no-audit', '--no-fund'], cwd=ROOT, check=True)
subprocess.run([sys.executable, str(ROOT / 'tools/build_flows.py'), '--native'], check=True)
secret_dir = Path('/etc/abc20261102')
secret_dir.mkdir(mode=0o700, exist_ok=True)
password = secrets.token_urlsafe(24)
js = "const b=require('./node_modules/bcryptjs');process.stdin.on('data',p=>process.stdout.write(b.hashSync(p.toString(),10)))"
hashed = subprocess.check_output(['node', '-e', js], input=password.encode(), cwd=ROOT).decode()
login = secret_dir / 'ui-login.json'
login.write_text(json.dumps({'user': 'lab', 'password': password, 'port': 28880}) + '\n')
login.chmod(0o600)
env = secret_dir / 'control.env'
env.write_text('ABC_UI_USER=lab\nABC_UI_PASSWORD_HASH=' + hashed + '\nABC_UI_PORT=28880\nABC_UI_HOST=127.0.0.1\nABC_FLOW_FILE=' + str(ROOT / 'deploy/nodered/flows-native.json') + '\n')
env.chmod(0o600)
(ROOT / 'state/node-red').mkdir(parents=True, exist_ok=True)
units = {
    'abc20261102-broker': f'''[Unit]
Description=Independent ABC experiment broker
After=network.target
[Service]
ExecStart=/usr/sbin/mosquitto -c {ROOT}/deploy/mosquitto-native.conf
Restart=on-failure
[Install]
WantedBy=multi-user.target
''',
    'abc20261102-console': f'''[Unit]
Description=Independent ABC experiment console
After=abc20261102-broker.service
Wants=abc20261102-broker.service
[Service]
WorkingDirectory={ROOT}
EnvironmentFile=/etc/abc20261102/control.env
ExecStart=/usr/bin/node {ROOT}/node_modules/node-red/red.js --userDir {ROOT}/state/node-red --settings {ROOT}/deploy/nodered/settings.js
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
'''}
for name, text in units.items():
    Path('/etc/systemd/system/' + name + '.service').write_text(text)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', *units], check=True)
print(json.dumps({'installed': list(units), 'ui_credentials_file': str(login), 'password_logged': False}))
