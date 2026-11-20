"""Restore BOTH project Supervisors after a restart. Idempotent.

The control cloud runs this through an SSH forced command named ``ensure``.
``start_gpu_native`` only restores the vision/LLM runtime; the audio worker has
its own Supervisor and its own SSH connection (28884), so a restart that only
calls the vision entry leaves audio dead. This entry restores both and reports
each half separately: a half that is not RUNNING must not be reported as done.

Rules:
  * Never start a second daemon when one is already serving the socket.
  * Only START programs whose state is STOPPED / EXITED / FATAL.
  * A healthy RUNNING program is left untouched (no restart, no bounce).
  * Failure of one half is reported, never swallowed into overall success.
"""
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/root/abc20261102')
AUDIO = Path('/root/mm_audio_v1')
AUDIO_PROGRAMS = ('audio-tunnel-28884', 'audio-worker')
DEADLINE_S = 55.0


def _ctl(root, ini):
    return [str(root / '.venv/bin/supervisorctl'), '-c', str(ini)]


def _status(ctl):
    """Return (running_dict, raw_text, returncode) from supervisorctl status."""
    r = subprocess.run(ctl + ['status'], capture_output=True, text=True)
    states = {}
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            states[fields[0]] = fields[1]
    return states, r.stdout + r.stderr, r.returncode


def _daemon_absent(text, returncode):
    if returncode == 0:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in
               ('no such file', 'refused', 'cannot connect', 'unix:///'))


def ensure_visual():
    sys.path.insert(0, str(ROOT / 'tools'))
    import start_gpu_native  # noqa: E402  (kept as the single vision/LLM owner)
    try:
        start_gpu_native.main()
        return True, 'vision runtime RUNNING'
    except Exception as exc:  # reported, not swallowed
        return False, 'vision runtime failed: %r' % (exc,)


def ensure_audio(deadline):
    ini = AUDIO / 'supervisord.ini'
    if not ini.is_file():
        return False, 'audio supervisor config missing: %s' % ini
    ctl = _ctl(ROOT, ini)
    states, text, rc = _status(ctl)
    if _daemon_absent(text, rc):
        # The socket is not served: start the daemon for THIS config only.
        subprocess.run([str(ROOT / '.venv/bin/supervisord'), '-c', str(ini)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        states, text, rc = _status(ctl)
    missing = [p for p in AUDIO_PROGRAMS if p not in states]
    if missing:
        return False, 'audio programs absent from supervisor: %s (%s)' % (
            ','.join(missing), text.strip()[:200])
    while time.monotonic() < deadline:
        states, text, rc = _status(ctl)
        restart = [p for p in AUDIO_PROGRAMS if states.get(p) in {'STOPPED', 'EXITED', 'FATAL'}]
        if restart:
            subprocess.run(ctl + ['start'] + restart,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            continue
        if all(states.get(p) == 'RUNNING' for p in AUDIO_PROGRAMS):
            return True, 'audio runtime RUNNING'
        if all(states.get(p) == 'STARTING' for p in AUDIO_PROGRAMS):
            time.sleep(2)
            continue
        return False, 'audio runtime in unexpected state: %s' % (
            {p: states.get(p) for p in AUDIO_PROGRAMS},)
    return False, 'audio runtime did not become ready: %s' % (
        {p: states.get(p) for p in AUDIO_PROGRAMS},)


def main():
    if ROOT != Path('/root/abc20261102'):
        raise SystemExit('Run only in the isolated GPU deployment')
    lock_path = ROOT / 'state' / 'ensure-all.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'project': ['abc20261102', 'mm_audio_v1'],
                              'result': 'busy', 'detail': 'another ensure run holds the lock'}))
            return 0
        deadline = time.monotonic() + DEADLINE_S
        visual_ok, visual_detail = ensure_visual()
        audio_ok, audio_detail = ensure_audio(deadline)
        payload = {
            'project': ['abc20261102', 'mm_audio_v1'],
            'result': 'RUNNING' if (visual_ok and audio_ok) else 'INCOMPLETE',
            'vision': {'ok': visual_ok, 'detail': visual_detail},
            'audio': {'ok': audio_ok, 'detail': audio_detail},
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if (visual_ok and audio_ok) else 1


if __name__ == '__main__':
    sys.exit(main())
