#!/bin/bash
# Apply the capture mixer profile to whichever USB card audio_device.py resolves.
#
# Why AGC is off: 'Auto Gain Control' is a compressor on the ADC input. Measured on this
# adapter on 2026-11-09 with the room silent, AGC on held the input at -35.0 dBFS RMS against
# -44.8 dBFS with AGC off -- it lifts the noise floor by ~10 dB during silence and then pulls
# the level back down while someone speaks. ASR and sound-event models need the input level to
# be a function of the scene, not of what the compressor did a second earlier, so gain is fixed
# here and the level is chosen by calibrate_gain.py against the real microphone.
set -u
cd /home/pi/mm_audio_v1 || exit 1

GAIN=${MM_CAPTURE_GAIN:-12}

echo "解析采集卡 ..."
.venv/bin/python - "$GAIN" <<'PY' || exit 1
import sys
import audio_device as ad

dev = ad.resolve()
print('  ' + dev.describe())
gain = int(sys.argv[1])
print('  AGC off: %s' % ('ok' if ad.set_agc(dev, False) else '该卡无此控件'))
if ad.set_capture_gain(dev, gain):
    print('  capture gain=%d: ok' % gain)
else:
    print('  capture gain 无法设置（该卡无音量控件）')
print('  读回 gain=%s' % ad.capture_gain(dev))
PY

echo "验证: 采集 3 秒并测量电平"
CARD=$(.venv/bin/python -c 'import audio_device as ad; print(ad.resolve().card)') || exit 1
arecord -q -D "hw:$CARD,0" -f S16_LE -r 48000 -c 1 -d 3 -t wav /tmp/gain_check.wav 2>/dev/null || exit 1
.venv/bin/python - /tmp/gain_check.wav <<'PY'
import math, struct, sys, wave
w = wave.open(sys.argv[1]); n = w.getnframes()
s = struct.unpack('<%dh' % n, w.readframes(n))
peak = max(abs(x) for x in s)
rms = math.sqrt(sum(x * x for x in s) / n)
print('  peak %6.1f dBFS | rms %6.1f dBFS  (静音时 rms 应低于 -45 dBFS)'
      % (20 * math.log10(peak / 32768 + 1e-12), 20 * math.log10(rms / 32768 + 1e-12)))
PY
