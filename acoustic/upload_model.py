"""Resumable, verified model transfer to an isolated host.

Rules enforced here:
  * transfer into a temporary directory, never into the live model file;
  * every chunk is verified by SHA256 against the local source;
  * the joined archive is verified by SHA256 before it is allowed to unpack;
  * every unpacked file is verified (size + SHA256) against MODEL_MANIFEST.json;
  * only then is the file moved into place atomically with os.replace;
  * a truncated/orphan archive left by an earlier attempt is never appended to.
"""
import hashlib
import json
import shlex
import sys
import time
from pathlib import Path

from remote import remote, out

ROOT = Path(__file__).resolve().parent
MANIFEST = {a['archive']: a for a in json.loads((ROOT / 'MODEL_MANIFEST.json').read_text('utf8'))}
STAGE = '/root/mm_audio_v1/models/.stage'
# The AutoDL container has no system python3; use the module venv that already exists.
REMOTE_PY = '/root/mm_audio_v1/.venv/bin/python'


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def upload(role, archive, chunk_size=4 << 20, timeout=300):
    src = ROOT / 'downloads' / archive
    meta = MANIFEST[archive]
    want = meta['archive_sha256']
    have = sha256_file(src)
    if have != want:
        raise SystemExit('local archive mismatch: %s != %s' % (have, want))
    size = src.stat().st_size
    if size != meta['archive_bytes']:
        raise SystemExit('local archive size mismatch')
    remote(role, 'mkdir -p %s' % STAGE)
    n = (size + chunk_size - 1) // chunk_size
    started = time.time()
    with open(src, 'rb') as f:
        for i in range(n):
            part = STAGE + '/%s.part%04d' % (archive, i)
            data = f.read(chunk_size)
            h = hashlib.sha256(data).hexdigest()
            for attempt in range(3):
                t = time.time()
                remote(role, 'cat > ' + part, data=data, timeout=timeout)
                got = out(role, 'sha256sum %s' % part).split()[0]
                if got == h:
                    break
                print('  part %d/%d mismatch, retry %d' % (i + 1, n, attempt + 1), flush=True)
            else:
                raise SystemExit('part %s failed verification' % part)
            print('  part %d/%d %5.1f%% %.1fs (%.2f MiB/s)' % (
                i + 1, n, 100.0 * (i + 1) / n, time.time() - t,
                len(data) / 1048576 / max(time.time() - t, 1e-6)), flush=True)
    joined = STAGE + '/' + archive
    remote(role, "cd %s && cat %s.part* > %s && sha256sum %s" % (
        STAGE, shlex.quote(archive), shlex.quote(archive), shlex.quote(archive)), timeout=600)
    got = out(role, 'sha256sum ' + joined).split()[0]
    if got != want:
        raise SystemExit('joined archive mismatch: %s != %s' % (got, want))
    print('joined archive verified %s in %.1fs' % (got, time.time() - started), flush=True)
    return joined


def unpack_and_install(role, archive):
    meta = MANIFEST[archive]
    joined = STAGE + '/' + archive
    remote(role, 'rm -rf %s/x && mkdir -p %s/x && tar -xjf %s -C %s/x' % (
        STAGE, STAGE, shlex.quote(joined), STAGE), timeout=900)
    for item in meta['files']:
        path = item['path']
        remote_path = '%s/x/%s' % (STAGE, path)
        line = out(role, 'stat -c %%s %s && sha256sum %s' % (
            shlex.quote(remote_path), shlex.quote(remote_path))).split('\n')
        got_bytes = int(line[0])
        got_hash = line[1].split()[0]
        ok = got_bytes == item['bytes'] and got_hash == item['sha256']
        print('  %s size=%d sha=%s %s' % (path, got_bytes, got_hash[:16], 'OK' if ok else 'MISMATCH'),
              flush=True)
        if not ok:
            raise SystemExit('extracted file failed verification: ' + path)
    for item in meta['files']:
        remote_path = '%s/x/%s' % (STAGE, item['path'])
        final = '/root/mm_audio_v1/models/' + item['path']
        command = (
            "mkdir -p %(d)s && chown root:root %(t)s && chmod 0644 %(t)s && "
            "%(py)s -c \"import os,sys;os.replace(sys.argv[1],sys.argv[2])\" %(t)s %(f)s && "
            "sync && stat -c '%%s %%n' %(f)s") % {
            'd': shlex.quote(str(Path(final).parent)),
            't': shlex.quote(remote_path), 'f': shlex.quote(final), 'py': REMOTE_PY}
        print('  install:', out(role, command, timeout=300).strip(), flush=True)
    remote(role, 'rm -rf %s/x %s/*.part* %s' % (STAGE, STAGE, shlex.quote(joined)), timeout=300)
    print('stage cleaned', flush=True)


if __name__ == '__main__':
    """usage: upload_model.py ROLE [ARCHIVE ...] [--install-only]

    --install-only skips the transfer and only re-runs verify-then-install on the
    archive already sitting in the remote staging directory."""
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    install_only = '--install-only' in sys.argv
    role = args[0] if args else 'gpu'
    archives = args[1:] or [a['archive'] for a in MANIFEST.values()]
    for archive in archives:
        print('== %s -> %s (install_only=%s)' % (archive, role, install_only), flush=True)
        if not install_only:
            upload(role, archive)
        unpack_and_install(role, archive)
