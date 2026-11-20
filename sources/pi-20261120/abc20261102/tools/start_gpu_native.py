"""Idempotently restore ONLY this project's Supervisor runtime after a restart."""
import fcntl
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS = {'mqtt-tunnel', 'ollama', 'vision-cloud', 'llm-adapter'}


def main():
    if ROOT != Path('/root/abc20261102'):
        raise SystemExit('Run only in the isolated GPU deployment')
    # A disabled direct-media route must not be revived by the 30-second watchdog.
    required = PROGRAMS | ({'media-broker'} if json.loads((ROOT / 'config.json').read_text()).get('media_enabled', False) else set())
    with (ROOT / 'state/gpu-ensure.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        control = [str(ROOT / '.venv/bin/supervisorctl'), '-c', str(ROOT / 'deploy/supervisord.ini')]
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            status = subprocess.run(control + ['status'], capture_output=True, text=True)
            states = {v[0]: v[1] for line in status.stdout.splitlines()
                      if len(v := line.split()) >= 2 and v[0] in required}
            if not states and status.returncode:
                if 'SHUTDOWN_STATE' not in status.stdout + status.stderr:
                    subprocess.run([str(ROOT / '.venv/bin/supervisord'), '-c', str(ROOT / 'deploy/supervisord.ini')],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(2)
                continue
            if set(states) == required and all(s == 'RUNNING' for s in states.values()):
                print(json.dumps({'project': 'abc20261102', 'runtime': 'RUNNING', 'programs': states}))
                return
            restart = [name for name, state in states.items() if state in {'FATAL', 'STOPPED', 'EXITED'}]
            if restart:
                subprocess.run(control + ['start'] + restart, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        raise RuntimeError('Independent GPU runtime did not become ready; inspect its own results logs')


if __name__ == '__main__':
    main()
