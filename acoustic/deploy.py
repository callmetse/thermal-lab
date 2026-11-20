"""Deploy this isolated module, leaving ABC files and services untouched.

Safety rules baked in:
  * nothing happens without an explicit --apply (a dry run only prints the plan);
  * only the new per-module Supervisor configs are read/started, never the ABC ones;
  * the control-cloud collector is started but never `systemctl enable`d, so a reboot
    does not silently bring the experiment back;
  * every deployed file's SHA256 is recorded for the delivery report.
"""
import hashlib
import io
import json
import shlex
import sys
import tarfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from remote import remote

ROOT=Path(__file__).resolve().parent
paths={'pi':'/home/pi/mm_audio_v1','gpu':'/root/mm_audio_v1','control':'/srv/mm_audio_v1'}
files=['common.py','edge.py','worker.py','collector.py','page.html']

def put(role,path,text):
    remote(role,'mkdir -p '+shlex.quote(str(Path(path).parent))+' && cat > '+shlex.quote(path),data=text.encode())

def supervisor(root,program,command,extra=''):
    return '''[unix_http_server]
file={r}/supervisor.sock
chmod=0600
[supervisord]
logfile={r}/results/supervisor.log
pidfile={r}/supervisor.pid
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{r}/supervisor.sock
[program:{p}]
command={c}
directory={r}
autostart=true
autorestart=true
startsecs=3
startretries=10
stopasgroup=true
killasgroup=true
stdout_logfile={r}/results/{p}.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true
{extra}
'''.format(r=root,p=program,c=command,extra=extra)

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


if __name__=='__main__':
    apply = '--apply' in sys.argv
    manifest = {f: digest(ROOT/f) for f in files}
    print('files', manifest, flush=True)
    if not apply:
        print('dry run: re-run with --apply to copy code and manage the new services', flush=True)
        raise SystemExit(0)
    # Refuse to deploy while an ABC experiment is running: the new module reuses the
    # ABC MQTT tunnel and must never disturb a live run.
    try:
        state = json.loads(remote('control', 'python3 /srv/abc20261102/tools/control_api.py state'))
    except Exception as ex:
        raise SystemExit('cannot read ABC control state, refusing to deploy: %s' % ex)
    if state.get('active'):
        raise SystemExit('an ABC experiment is active; stop it before deploying')
    print('ABC idle: active=null', flush=True)
    for role,root in paths.items():
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w:gz') as tar:
            for f in files:tar.add(ROOT/f,arcname=f)
        remote(role,'mkdir -p '+root+'/results && tar -xzf - -C '+root,data=buf.getvalue())
        print(role+' code copied',flush=True)
    # New authenticated gateway. Private authentication configuration is reused read-only.
    put('pi',paths['pi']+'/Caddyfile','''{
admin off
auto_https off
persist_config off
}
http://:28980 {
 bind 0.0.0.0
 @lan remote_ip private_ranges
 handle @lan {
  basic_auth {
   {$ABC_CONSOLE_USER} {$ABC_CONSOLE_HASH}
  }
  @mm path /mm /mm/*
  handle @mm { 
   reverse_proxy 127.0.0.1:28991
  }
  @video path /live-video
  handle @video {
   reverse_proxy 127.0.0.1:28990 {
    flush_interval -1
   }
  }
  @visual path /api/*
  handle @visual {
   reverse_proxy 127.0.0.1:28881
  }
  handle {
   redir / /mm 302
  }
 }
 handle {
  respond "Local network only" 403
 }
}
''')
    put('pi',paths['pi']+'/gateway.sh','''#!/bin/sh
set -eu
set -a
. /home/pi/.config/abc20261102/console-auth.env
set +a
exec /home/pi/abc20261102/runtime/viewer/caddy run --config /home/pi/mm_audio_v1/Caddyfile --adapter caddyfile
''')
    for role in ('pi','gpu'):
        root=paths[role]
        program='audio-edge' if role=='pi' else 'audio-worker'
        command=root+'/.venv/bin/python -u '+root+('/edge.py' if role=='pi' else '/worker.py')
        extra=''
        if role=='pi':
            extra='''[program:audio-gateway]
command=/bin/sh /home/pi/mm_audio_v1/gateway.sh
directory=/home/pi/mm_audio_v1
environment=XDG_DATA_HOME="/home/pi/mm_audio_v1/state/caddy-data",XDG_CONFIG_HOME="/home/pi/mm_audio_v1/state/caddy-config"
autostart=true
autorestart=true
startsecs=2
stdout_logfile=/home/pi/mm_audio_v1/results/gateway.log
redirect_stderr=true
'''
        put(role,root+'/supervisord.ini',supervisor(root,program,command,extra))
        old='/home/pi/abc20261102' if role=='pi' else '/root/abc20261102'
        # Idempotent new-service management only.
        cmd=old+'/.venv/bin/supervisorctl -c '+root+'/supervisord.ini pid'
        try:
            print(remote(role,cmd).decode(),flush=True)
        except RuntimeError:
            print(remote(role,old+'/.venv/bin/supervisord -c '+root+'/supervisord.ini').decode(),flush=True)
        else:
            try:
                print(remote(role,old+'/.venv/bin/supervisorctl -c '+root+'/supervisord.ini reread').decode(),flush=True)
                print(remote(role,old+'/.venv/bin/supervisorctl -c '+root+'/supervisord.ini update').decode(),flush=True)
                print(remote(role,old+'/.venv/bin/supervisorctl -c '+root+'/supervisord.ini restart all').decode(),flush=True)
            except RuntimeError as ex:
                print('New service waiting for readiness:',str(ex),flush=True)
    # Collector uses a new venv and a new systemd unit on the existing control host.
    remote('control','python3 -m venv --without-pip /srv/mm_audio_v1/.venv && pip --python /srv/mm_audio_v1/.venv/bin/python install --disable-pip-version-check paho-mqtt==2.1.0 > /srv/mm_audio_v1/results/install.log 2>&1',timeout=120)
    put('control','/etc/systemd/system/mm-audio-collector.service','''[Unit]
Description=Isolated multimodal event collector
After=network.target
[Service]
WorkingDirectory=/srv/mm_audio_v1
ExecStart=/srv/mm_audio_v1/.venv/bin/python -u /srv/mm_audio_v1/collector.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
''')
    # Start only. Boot autostart stays disabled until the user explicitly asks for it.
    print(remote('control','systemctl daemon-reload && systemctl start mm-audio-collector.service && systemctl is-active mm-audio-collector.service && systemctl is-enabled mm-audio-collector.service || true').decode(),flush=True)
