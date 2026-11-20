"""Install the isolated Pi LAN entry; credentials remain outside the repository."""
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import urllib.request
import base64
import hashlib
import time

from ssh_access import connection

ROOT = Path(__file__).resolve().parent.parent
SSH = Path('/mnt/c/Users/operator/.ssh')
PI_ROOT = '/home/pi/abc20261102'
CONTROL_ROOT = '/srv/abc20261102'
URL = 'http://192.0.2.11:28880/lab'


def checked(run, command, data=None, timeout=30):
    result = run(command, data=data, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Deployment step failed: ' + command.split(';')[0])
    return result.stdout


def main():
    with connection('192.0.2.11', 'pi', 22, SSH / 'id_ed25519', SSH / 'known_hosts') as pi, \
         connection('192.0.2.10', 'root', 22, SSH / 'control-example', SSH / 'known_hosts') as control:
        state = json.loads(checked(control, 'python3 /srv/abc20261102/tools/control_api.py state'))
        if state.get('active'):
            raise RuntimeError('A run is active; do not restart the console during capture')
        # Read the existing bcrypt hash without displaying it or writing it into project files.
        code = "import json;from pathlib import Path;e=dict(l.split('=',1) for l in Path('/etc/abc20261102/control.env').read_text().splitlines() if '=' in l);print(json.dumps({'user':e['ABC_UI_USER'],'hash':e['ABC_UI_PASSWORD_HASH']}))"
        hashed = json.loads(checked(control, 'python3 -c ' + shlex.quote(code)))
        auth = json.loads(checked(control, 'cat /etc/abc20261102/ui-login.json'))
        private_env = ('ABC_CONSOLE_USER=' + shlex.quote(hashed['user']) + '\nABC_CONSOLE_HASH=' + shlex.quote(hashed['hash']) + '\n').encode()
        checked(pi, 'mkdir -p /home/pi/.config/abc20261102; chmod 700 /home/pi/.config/abc20261102; umask 077; cat > /home/pi/.config/abc20261102/console-auth.env', private_env)
        checked(pi, 'test -f /home/pi/.ssh/abc20261102_console || ssh-keygen -q -t ed25519 -N \'\' -C abc20261102-console -f /home/pi/.ssh/abc20261102_console')
        public = checked(pi, 'cat /home/pi/.ssh/abc20261102_console.pub').decode().strip()
        if not public.startswith('ssh-ed25519 '):
            raise RuntimeError('Missing dedicated console identity')
        entry = 'restrict,port-forwarding,permitopen="127.0.0.1:28880",command="/bin/false" ' + public
        code = "from pathlib import Path;p=Path('/root/.ssh/authorized_keys');s=p.read_text();entry=" + repr(entry) + ";key=entry.split('ssh-ed25519 ',1)[1].split()[0];p.write_text(s.rstrip()+'\\n'+entry+'\\n') if key not in s else None;p.chmod(0o600)"
        checked(control, 'python3 -c ' + shlex.quote(code))
        archive_path = ROOT / 'downloads/caddy-linux-arm64.tar.gz'
        provenance = json.loads((ROOT / 'results/lan-caddy-provenance.json').read_text())
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != provenance['sha256']:
            raise RuntimeError('Official Caddy archive differs from the verified artifact')
        with tarfile.open(archive_path, mode='r:gz') as archive:
            member = next(m for m in archive if m.name in ('caddy', './caddy'))
            binary = archive.extractfile(member).read()
        checked(pi, 'mkdir -p ' + PI_ROOT + '/runtime/viewer; cat > ' + PI_ROOT + '/runtime/viewer/caddy', binary)
        checked(pi, 'chmod 755 ' + PI_ROOT + '/runtime/viewer/caddy')
        for filename in ['deploy/console.Caddyfile', 'deploy/run_console_gateway.sh', 'tools/build_supervisor.py']:
            checked(pi, 'cat > ' + PI_ROOT + '/' + filename, (ROOT / filename).read_bytes())
        checked(pi, 'set -a; . /home/pi/.config/abc20261102/console-auth.env; set +a; ' + PI_ROOT + '/runtime/viewer/caddy validate --config ' + PI_ROOT + '/deploy/console.Caddyfile --adapter caddyfile')
        checked(pi, 'cd ' + PI_ROOT + ' && .venv/bin/python tools/build_supervisor.py edge')
        supervisor = PI_ROOT + '/.venv/bin/supervisorctl -c ' + PI_ROOT + '/deploy/supervisord.ini '
        checked(pi, supervisor + 'reread')
        checked(pi, supervisor + 'update console-tunnel console-gateway')
        checked(control, 'cat > ' + CONTROL_ROOT + '/deploy/nodered/page.html', (ROOT / 'deploy/nodered/page.html').read_bytes())
        checked(control, 'cd ' + CONTROL_ROOT + ' && python3 tools/build_flows.py --native && systemctl restart abc20261102-console')
        authorization = 'Basic ' + base64.b64encode((auth['user'] + ':' + auth['password']).encode()).decode()
        for attempt in range(40):
            try:
                request = urllib.request.Request(URL.replace('/lab', '/api/state'), headers={'Authorization': authorization})
                with urllib.request.urlopen(request, timeout=3) as response:
                    json.load(response)
                break
            except Exception:
                if attempt == 39:
                    raise RuntimeError('Pi LAN console did not become reachable')
                time.sleep(.5)
        # Give the account owner a private local login note, never a chat/log password.
        powershell = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
        script = r"""$p=Join-Path $env:USERPROFILE 'thermal-lab/private';[void][IO.Directory]::CreateDirectory($p);$id=[Security.Principal.WindowsIdentity]::GetCurrent();$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($id.User);$acl.SetAccessRuleProtection($true,$false);$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit';$rule=New-Object Security.AccessControl.FileSystemAccessRule($id.User,'FullControl',$inherit,'None','Allow');$acl.AddAccessRule($rule);$system=New-Object Security.Principal.SecurityIdentifier('S-1-5-18');$rule2=New-Object Security.AccessControl.FileSystemAccessRule($system,'FullControl',$inherit,'None','Allow');$acl.AddAccessRule($rule2);Set-Acl -LiteralPath $p -AclObject $acl"""
        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True)
        if result.returncode:
            raise RuntimeError('Could not prepare a private local login directory')
        secret = Path('/etc/thermal-lab/private/operator.json')
        secret.write_text(json.dumps({'url': URL, 'username': auth['user'], 'password': auth['password']}, ensure_ascii=False, indent=2))
        print(json.dumps({'url': URL, 'private_login_file': str(secret), 'password_logged': False}))


if __name__ == '__main__':
    main()
