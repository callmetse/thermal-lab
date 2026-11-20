"""Deployment-only SSH helper. Keys stay outside the experiment package."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile


@contextmanager
def connection(host, user, port, key_path, known_hosts, local_forward=None):
    with tempfile.TemporaryDirectory(prefix="abc-ssh-") as d:
        key = Path(d) / "key"
        shutil.copyfile(key_path, key)
        key.chmod(0o600)
        base = ["ssh", "-F", "/dev/null", "-p", str(port), "-i", str(key),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
                user + "@" + host]
        def run(command, data=None, timeout=60):
            return subprocess.run(base + [command], input=data, capture_output=True, timeout=timeout)
        tunnel = None
        try:
            if local_forward:
                local_port, remote_port = local_forward
                tunnel = subprocess.Popen(base[:-1] + ['-N', '-T', '-o', 'ExitOnForwardFailure=yes',
                    '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', base[-1]],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            yield run
        finally:
            if tunnel:
                tunnel.terminate()
                try:
                    tunnel.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    tunnel.kill()
                    tunnel.wait()
