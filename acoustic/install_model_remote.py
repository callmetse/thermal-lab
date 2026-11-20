"""Download a model archive on the target host, verify it, then install atomically.

Used instead of a local upload when the measured upload throughput is far worse
than the host's own download throughput. Trust does not shift: the archive must
match MODEL_MANIFEST.json exactly (size + SHA256) before anything is unpacked,
and every unpacked file is verified again before an atomic os.replace.
"""
import json
import shlex
import sys
from pathlib import Path

from remote import remote, out
from upload_model import MANIFEST, STAGE, unpack_and_install

ROOT = Path(__file__).resolve().parent
RELEASES = 'https://github.com/k2-fsa/sherpa-onnx/releases/download'


def source_url(archive):
    if archive.startswith('sherpa-onnx-sense-voice'):
        return RELEASES + '/asr-models/' + archive
    return RELEASES + '/audio-tagging-models/' + archive


def fetch(role, archive):
    meta = MANIFEST[archive]
    want, size = meta['archive_sha256'], meta['archive_bytes']
    url = source_url(archive)
    final = STAGE + '/' + archive
    dl = final + '.dl'
    command = r'''
set -eu
cd {stage}
url={url}
want={want}
want_bytes={want_bytes}
target={dl}
for attempt in 1 2 3 4 5 6; do
  have=$(stat -c %s "$target" 2>/dev/null || echo 0)
  if [ "$have" -lt "$want_bytes" ]; then
    curl -sS -L --fail --max-time 900 -C - -o "$target" "$url" || true
  fi
  size=$(stat -c %s "$target" 2>/dev/null || echo 0)
  echo "attempt $attempt size $size / $want_bytes"
  if [ "$size" -eq "$want_bytes" ]; then
    got=$(sha256sum "$target" | cut -d' ' -f1)
    echo "sha256 $got"
    if [ "$got" = "$want" ]; then
      mv -f "$target" {final}
      echo VERIFIED
      exit 0
    fi
    echo "hash mismatch; discarding partial"
    rm -f "$target"
  fi
  sleep 3
done
echo FAILED
exit 1
'''.format(stage=shlex.quote(STAGE), url=shlex.quote(url), want=want,
             want_bytes=size, dl=shlex.quote(dl), final=shlex.quote(final))
    print('downloading on %s ...' % role, flush=True)
    print(out(role, command, timeout=1800).strip()[-400:], flush=True)
    got = out(role, 'stat -c %%s %s && sha256sum %s' % (shlex.quote(final), shlex.quote(final)))
    line = got.split('\n')
    if int(line[0]) != size or line[1].split()[0] != want:
        raise SystemExit('archive verification failed on host')
    print('archive verified: %s (%d bytes)' % (want, size), flush=True)
    unpack_and_install(role, archive)


if __name__ == '__main__':
    role = sys.argv[1] if len(sys.argv) > 1 else 'gpu'
    for archive in sys.argv[2:]:
        print('== %s on %s' % (archive, role), flush=True)
        fetch(role, archive)
