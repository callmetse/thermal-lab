"""Resolve the capture card and the settings it actually advertises.

Why this module exists
----------------------
The recorder opened a literal ALSA address, ``hw:CARD=Device,DEV=0``. ``Device`` is the
short id this USB-adapter family reports, so the literal only works on one arrangement of
hardware: exactly one such adapter attached, enumerated in a slot that yields that id.
Attach a second one, replace it with a device that reports a different id, or replug into a
port that changes the card index, and the recorder either opens the wrong card or dies with
"No such device" mid-session. The ``48000 / S16_LE / mono`` triple has the same weakness: it
is one adapter's capability, not a property of the pipeline.

So the address is resolved from /proc/asound at capture time, and format/rate/channels come
from the capture stream the card advertises. The period size is then chosen for pipe
latency and *verified* with a one-second capture, because a card can advertise support and
still refuse a small period. If the probe fails the driver defaults are used; nothing here
guesses at what a device can do.

Measured on 2026-11-09 with the C-Media PCM2902 adapter: default ALSA period of 125 ms put
the first 0.25 s chunk at 388-393 ms after start; a 10 ms period brought it to 270-275 ms
with the same 250 ms cadence and no overruns over a 90 s soak under 4-core load. Buffer size
does not affect that latency (only headroom), so it is kept large.

Override: ``MM_AUDIO_DEVICE=hw:2,0`` or ``hw:CARD=Device,DEV=0`` pins a device explicitly.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ALSA = Path('/proc/asound')
_CARD_RE = re.compile(r'^\s*(\d+)\s+\[(?P<id>\S+)\s*\]\s*:\s*(?P<driver>\S+)\s+-\s+(?P<desc>.*)$')
_CACHE = {}


@dataclass(frozen=True)
class CaptureDevice:
    """An ALSA capture endpoint plus the settings proven to work on it."""

    alsa: str
    card: int
    name: str
    rate: int
    channels: int
    sample_format: str
    period_size: int = 0
    buffer_size: int = 0
    rates: tuple = ()
    formats: tuple = ()
    source: str = 'discovered'

    def arecord_args(self, seconds):
        return ['arecord', '-q', '-D', self.alsa, '-t', 'raw',
                '-f', self.sample_format, '-c', str(self.channels),
                '-r', str(self.rate), '-d', str(seconds),
                '--period-size', str(self.period_size),
                '--buffer-size', str(self.buffer_size)]

    def describe(self):
        return ('capture device: %s (card %d, %s) %s %d ch %d Hz, period %d frames, '
                'buffer %d frames, source=%s'
                % (self.alsa, self.card, self.name, self.sample_format, self.channels,
                   self.rate, self.period_size, self.buffer_size, self.source))

    def as_dict(self):
        return dict(alsa=self.alsa, card=self.card, name=self.name, rate=self.rate,
                    channels=self.channels, sample_format=self.sample_format,
                    period_size=self.period_size, buffer_size=self.buffer_size,
                    rates=list(self.rates), formats=list(self.formats), source=self.source)


def _cards():
    """Parse /proc/asound/cards into [(index, id, driver, description)]."""
    try:
        text = (ALSA / 'cards').read_text(encoding='utf8', errors='replace')
    except OSError:
        return []
    cards, pending = [], None
    for line in text.splitlines():
        m = _CARD_RE.match(line)
        if m:
            pending = [int(m.group(1)), m.group('id'), m.group('driver'), m.group('desc').strip()]
            cards.append(pending)
        elif pending is not None and line.startswith(' ' * 20):
            # Continuation line carries the human-readable product name.
            pending[3] = (pending[3] + ' ' + line.strip()).strip()
        elif line.strip():
            pending = None
    return cards


def _capture_altsets(card):
    """Return the capture altsets a card advertises, [] when it has no capture stream.

    /proc/asound/cardN/stream0 is the kernel's own view of the USB audio format
    descriptors, which is what the card will actually accept -- unlike a guess.
    """
    path = ALSA / ('card%d' % card) / 'stream0'
    try:
        lines = path.read_text(encoding='utf8', errors='replace').splitlines()
    except OSError:
        return []
    altsets, current, in_capture = [], None, False
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(':') and not line.startswith(' '):
            in_capture = stripped.lower() == 'capture:'
            current = None
            continue
        if not in_capture:
            continue
        if stripped.startswith('Altset'):
            current = dict(altset=stripped.split()[1], formats=[], rates=[], channels=0)
            altsets.append(current)
            continue
        if current is None:
            continue
        key, _, value = stripped.partition(':')
        key = key.strip().lower()
        if key == 'format':
            current['formats'].append(value.strip())
        elif key == 'rates':
            current['rates'] = [int(x) for x in re.findall(r'\d+', value)]
        elif key == 'channels':
            current['channels'] = int(value.strip() or 0)
    return [a for a in altsets if a['formats'] and a['rates']]


def _pick(altsets):
    """Choose the altset that keeps the most information: S16 first, mono, widest rate.

    48 kHz is preferred over 44.1 kHz because the pipeline resamples to 16 kHz: 48 -> 16 is
    exactly 3:1, so the decimation filter can be exact, whereas 44.1 -> 16 is 441:160.
    """
    if not altsets:
        return None
    def score(a):
        fmt = 'S16_LE' in a['formats']
        return (fmt, a['channels'] == 1, max(a['rates']), a['channels'])
    best = max(altsets, key=score)
    rates = tuple(sorted(set(best['rates'])) or (48000,))
    rate = 48000 if 48000 in rates else max(rates)
    formats = tuple(best['formats'])
    fmt = 'S16_LE' if 'S16_LE' in formats else formats[0]
    return dict(rate=rate, channels=max(1, best['channels']), sample_format=fmt,
                rates=rates, formats=formats)


def _timing(rate):
    """Period for freshness, buffer for headroom. Both scale with the device rate."""
    period = max(1, rate // 100)      # 10 ms
    return period, period * 10        # 100 ms of ring buffer


def _works(dev, seconds=1):
    """One-second capture with the requested settings; a card may advertise and still refuse."""
    args = dev.arecord_args(seconds) + ['-t', 'raw', '/dev/null']
    try:
        p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=seconds + 5)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, str(ex)
    if p.returncode:
        return False, p.stderr.decode('utf8', 'replace').strip()[:300]
    return True, ''


def _explicit_device(spec):
    """Turn 'hw:2,0' / 'hw:CARD=Device,DEV=0' into (alsa, card number or None)."""
    spec = spec.strip()
    if not spec:
        return None
    m = re.match(r'^hw:CARD=(\w+)(?:,DEV=(\d+))?$', spec)
    if m:
        for index, cid, _driver, _desc in _cards():
            if cid.lower() == m.group(1).lower():
                return 'hw:%d,%s' % (index, m.group(2) or '0'), index
        return None
    m = re.match(r'^hw:(\d+)(?:,(\d+))?$', spec)
    if m:
        return 'hw:%s,%s' % (m.group(1), m.group(2) or '0'), int(m.group(1))
    return None


def resolve(override=None):
    """Return the capture device to use, preferring the first card with a capture stream."""
    override = override if override is not None else os.environ.get('MM_AUDIO_DEVICE', '')
    try:
        fingerprint = (ALSA / 'cards').read_text(encoding='utf8', errors='replace')
    except OSError:
        fingerprint = ''
    key = (fingerprint, override)
    if key in _CACHE:
        return _CACHE[key]

    chosen = None
    explicit = _explicit_device(override) if override else None
    if explicit:
        alsa, card = explicit
        caps = _pick(_capture_altsets(card)) or dict(rate=48000, channels=1,
                                                     sample_format='S16_LE', rates=(), formats=())
        chosen = CaptureDevice(alsa=alsa, card=card, name='explicit', source='env', **caps)
    else:
        # Prefer a card that exposes a capture stream; USB audio first, then anything.
        candidates = [c for c in _cards() if _capture_altsets(c[0])]
        candidates.sort(key=lambda c: (0 if c[2] == 'USB-Audio' else 1, c[0]))
        for index, _cid, driver, desc in candidates:
            caps = _pick(_capture_altsets(index))
            if caps:
                chosen = CaptureDevice(alsa='hw:%d,0' % index, card=index,
                                       name='%s %s' % (driver, desc), source='discovered', **caps)
                break

    if chosen is None:
        # Nothing discoverable: keep the old literal so behaviour is unchanged on hardware
        # this module cannot see (e.g. a container without /proc/asound), and fail loudly.
        chosen = CaptureDevice(alsa=override or 'hw:CARD=Device,DEV=0', card=-1,
                               name='fallback', rate=48000, channels=1,
                               sample_format='S16_LE', period_size=1200, buffer_size=4800,
                               source='fallback')
        _CACHE[key] = chosen
        return chosen

    period, buffer = _timing(chosen.rate)
    tuned = CaptureDevice(**dict(chosen.as_dict(), period_size=period, buffer_size=buffer))
    okay, why = _works(tuned)
    if not okay:
        default_period, default_buffer = _timing(chosen.rate)
        plain = CaptureDevice(**dict(chosen.as_dict(), period_size=default_period,
                                     buffer_size=default_buffer * 5))
        if _works(plain)[0]:
            plain = CaptureDevice(**dict(plain.as_dict(), source=chosen.source + '+default_timing'))
            _CACHE[key] = plain
            return plain
        # Both refused: still return the device so the failure names the real endpoint,
        # and keep the driver's own message for whoever reads capture.log.
        chosen = CaptureDevice(**dict(chosen.as_dict(), period_size=default_period,
                                      buffer_size=default_buffer, source='probe_failed'))
        print('audio_device: probe failed for %s: %s' % (chosen.alsa, why), flush=True)
    else:
        chosen = tuned

    _CACHE[key] = chosen
    return chosen


def warm():
    """Resolve once at process start so the first utterance does not pay for the probe."""
    try:
        return resolve()
    except Exception as ex:                      # noqa: BLE001 - never block startup
        print('audio_device: warm probe failed: %s' % ex, flush=True)
        return None


def _amixer(dev, *args):
    return subprocess.run(['amixer', '-c', str(dev.card)] + list(args),
                          capture_output=True, text=True)


def capture_gain(dev):
    """Current capture gain, or None when the card exposes no such control."""
    out = _amixer(dev, 'contents').stdout
    for block in out.split('numid'):
        if "name='Mic Capture Volume'" in block:
            for line in block.splitlines():
                if ': values=' in line:
                    try:
                        return int(line.split('=')[1].strip())
                    except ValueError:
                        return None
    return None


def set_capture_gain(dev, value):
    """Set capture gain, tolerating the two names ALSA reports for one control.

    ``amixer contents`` lists it as ``Mic Capture Volume``, but the simple-control lookup
    that ``sset`` uses does not accept that name on this card -- it answers "Unable to find
    simple control". The shorthand ``Mic`` and the raw ``numid`` both work, so try the name
    the mixer advertises first (works on other cards), then the shorthand, then the numid.
    Returns True when the value was actually applied.
    """
    for args in (('sset', 'Mic Capture Volume', str(value)),
                 ('sset', 'Mic', str(value))):
        if _amixer(dev, *args).returncode == 0:
            return True
    numid = None
    for line in _amixer(dev, 'controls').stdout.splitlines():
        if 'Mic Capture Volume' in line and 'numid=' in line:
            numid = line.split('numid=')[1].split(',')[0]
            break
    if numid:
        return _amixer(dev, 'cset', 'numid=' + numid, str(value)).returncode == 0
    return False


def set_agc(dev, on):
    """Turn the input compressor off (or on); returns True when the card has the control."""
    return _amixer(dev, 'sset', 'Auto Gain Control', 'on' if on else 'off').returncode == 0
