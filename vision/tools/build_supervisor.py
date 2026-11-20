"""Use mature Supervisor for the native GPU container and Pi worker lifecycle."""
import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument('role', choices=['edge', 'cloud'])
a = p.parse_args()
media_autostart = str(json.loads((root / 'config.json').read_text()).get('media_enabled', False)).lower()
home = root.parent
(root / 'state').mkdir(exist_ok=True)
text = f'''[unix_http_server]
file={root}/state/supervisor.sock
chmod=0600
[supervisord]
logfile={root}/results/supervisor.log
pidfile={root}/state/supervisor.pid
childlogdir={root}/results
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{root}/state/supervisor.sock
[program:mqtt-tunnel]
priority=10
command=/usr/bin/ssh -N -T -F /dev/null -i {home}/.ssh/abc20261102_mqtt -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={home}/.ssh/known_hosts_abc20261102 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:28883:127.0.0.1:28883 root@192.0.2.10
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/mqtt-tunnel.log
redirect_stderr=true
[program:vision-{a.role}]
priority=20
command={root}/.venv/bin/python -m lab.worker {a.role}
directory={root}
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/worker-{a.role}.log
redirect_stderr=true
'''
if a.role == 'edge':
    text += f'''[program:media-tunnel]
autostart={media_autostart}
command=/usr/bin/ssh -N -T -F /dev/null -p 22 -i {home}/.ssh/abc20261102_media -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={home}/.ssh/known_hosts_abc20261102_media -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:28583:127.0.0.1:28583 root@gpu.example.invalid
priority=10
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/media-tunnel.log
redirect_stderr=true
[program:console-tunnel]
command=/usr/bin/ssh -N -T -F /dev/null -i {home}/.ssh/abc20261102_console -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={home}/.ssh/known_hosts_abc20261102 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:28881:127.0.0.1:28880 root@192.0.2.10
priority=10
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/console-tunnel.log
redirect_stderr=true
[program:console-gateway]
command=/bin/sh {root}/deploy/run_console_gateway.sh
directory={root}
environment=XDG_DATA_HOME="{root}/state/caddy-data",XDG_CONFIG_HOME="{root}/state/caddy-config"
priority=20
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/console-gateway.log
redirect_stderr=true
'''
if a.role == 'cloud':
    text += f'''[program:media-broker]
autostart={media_autostart}
priority=5
command={root}/runtime/mosquitto/sbin/mosquitto -c {root}/deploy/media-mosquitto.conf
directory={root}
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/media-broker.log
redirect_stderr=true
[program:llm-adapter]
priority=30
command={root}/.venv/bin/python -m lab.llm
directory={root}
autorestart=true
startretries=30
startsecs=2
stdout_logfile={root}/results/llm-adapter.log
redirect_stderr=true
[program:ollama]
priority=10
command={root}/runtime/bin/ollama serve
directory={root}
environment=OLLAMA_HOST="127.0.0.1:28434",OLLAMA_MODELS="{root}/state/ollama-models",OLLAMA_NUM_PARALLEL="1",OLLAMA_MAX_LOADED_MODELS="1"
autorestart=true
startretries=30
startsecs=3
stopasgroup=true
killasgroup=true
stdout_logfile={root}/results/ollama-supervised.log
redirect_stderr=true
'''
(root / 'deploy/supervisord.ini').write_text(text)
print('Generated isolated Supervisor config for', a.role)
