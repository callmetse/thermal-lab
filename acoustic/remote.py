"""Thin SSH wrapper for the isolated multimodal module.

Reuses the existing keys and known_hosts that already live in the user's
private SSH directory (`~/.ssh`). Credentials are never copied into this
project, printed, logged, or shipped to any device.

Uses the platform's own ssh client because on this Windows host the MSYS
ssh cannot resolve the GPU relay name while the Windows OpenSSH client can.
Keys are passed by path only, so no key material is ever duplicated.
"""
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

KEYS = Path(os.environ.get('THERMAL_SSH_DIR', Path.home() / '.ssh'))

if os.name == 'nt':
    SSH = r'C:\Windows\System32\OpenSSH\ssh.exe'
else:
    SSH = '/usr/bin/ssh'

HOSTS = {
    'pi': ('192.0.2.11', 'pi', 22, 'id_ed25519', '/home/pi/abc20261102'),
    'gpu': ('gpu.example.invalid', 'root', 22, 'gpu-example', '/root/abc20261102'),
    'control': ('192.0.2.10', 'root', 22, 'control-example', '/srv/abc20261102'),
}


def _base(role):
    host, user, port, key, _root = HOSTS[role]
    return [SSH, '-F', os.devnull if os.name == 'nt' else '/dev/null',
            '-p', str(port), '-i', str(KEYS / key),
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(KEYS / 'known_hosts'),
            user + '@' + host]


def remote(role, command, data=None, timeout=90, retries=3):
    base = _base(role)
    r = None
    for attempt in range(retries):
        r = subprocess.run(base + [command], input=data, capture_output=True, timeout=timeout)
        if r.returncode != 255:
            break
        time.sleep(2)
    if r.returncode:
        raise RuntimeError(role + ': rc=' + str(r.returncode) + ' '
                           + r.stderr.decode(errors='replace')[-800:]
                           + r.stdout.decode(errors='replace')[-800:])
    return r.stdout


def out(role, command, timeout=90):
    return remote(role, command, timeout=timeout).decode(errors='replace')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'put':
        _role, _local, _remote = sys.argv[2], sys.argv[3], sys.argv[4]
        _data = Path(_local).read_bytes()
        _cmd = 'mkdir -p %s && cat > %s' % (shlex.quote(str(Path(_remote).parent)),
                                            shlex.quote(_remote))
        remote(_role, _cmd, data=_data, timeout=300)
        print('put %s -> %s:%s (%d bytes)' % (_local, _role, _remote, len(_data)))
        print(out(_role, ' && '.join(sys.argv[5:]) if len(sys.argv) > 5 else 'ls -l ' + shlex.quote(_remote)))
    else:
        print(out(sys.argv[1], ' '.join(sys.argv[2:]), timeout=int(os.environ.get('SSH_TIMEOUT', '90'))))
