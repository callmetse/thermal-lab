"""Patch edge.py to resolve the capture device instead of naming it literally.

Refuses to write unless every anchor matches exactly once, keeps a timestamped backup, and
byte-compiles the result so a syntax error cannot reach a running deployment.
"""
import py_compile
import re
import shutil
import sys
import time
from pathlib import Path

TARGET = Path('/home/pi/mm_audio_v1/edge.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')
BACKUP = Path('/home/pi/mm_audio_v1/results') / ('backup_audio_device_%s' % STAMP)


def sub_once(pattern, replacement, text, label):
    """Apply one anchored substitution, refusing anything ambiguous."""
    new, count = re.subn(pattern, replacement, text, flags=re.M | re.S)
    if count != 1:
        sys.exit('ABORT: anchor %r matched %d times (expected 1); file untouched' % (label, count))
    return new


def main():
    text = TARGET.read_text(encoding='utf8')
    original = text

    text = sub_once(r'^(?P<i>[ ]+)p = subprocess\.Popen\(\[\'arecord\'.*?stderr=err\)\n',
                    lambda m: (
                        m.group('i') + 'dev = resolve_capture()\n'
                        + m.group('i') + "err.write((dev.describe() + '\\n').encode()); err.flush()\n"
                        + m.group('i') + 'p = subprocess.Popen(dev.arecord_args(seconds), '
                        + 'stdout=subprocess.PIPE, stderr=err)\n'),
                    text, 'arecord block')

    text = sub_once(r'^(?P<i>[ ]+)ff = subprocess\.Popen\(\[\'ffmpeg\'.*?stderr=err\)\n',
                    lambda m: (
                        m.group('i') + "ff = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error',\n"
                        + m.group('i') + "     '-probesize', '32', '-analyzeduration', '0', '-f', 's16le',\n"
                        + m.group('i') + "     '-ar', str(dev.rate), '-ac', str(dev.channels),\n"
                        + m.group('i') + "     '-i', 'pipe:0', '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1'],\n"
                        + m.group('i') + '     stdin=p.stdout, stdout=subprocess.PIPE, stderr=err)\n'),
                    text, 'ffmpeg block')

    text = sub_once(r'hardware_sample_rate=48000\)',
                    'hardware_sample_rate=dev.rate)',
                    text, 'header sample rate')

    text = sub_once(r'(?m)^( +journal, mqtt, pack, publish\)\n)',
                    r'\1from audio_device import warm as warm_capture, resolve as resolve_capture\n',
                    text, 'import')

    text = sub_once(r'(?m)^(def main\(\):\n)(    e = Edge\(\))',
                    lambda m: (m.group(1)
                               + '    warm_capture()   # one probe, off the capture path\n'
                               + m.group(2)),
                    text, 'main warm-up')

    if 'hw:CARD=Device' in text:
        sys.exit('ABORT: literal device address still present')

    BACKUP.mkdir(parents=True, exist_ok=True)
    backup = BACKUP / 'edge.py'
    shutil.copy2(TARGET, backup)
    TARGET.write_text(text, encoding='utf8')
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as ex:
        TARGET.write_text(original, encoding='utf8')
        sys.exit('ABORT: patched file does not compile, rolled back: %s' % ex)
    print('patched %s (backup %s)' % (TARGET, backup))


if __name__ == '__main__':
    main()
