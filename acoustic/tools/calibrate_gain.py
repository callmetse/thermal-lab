"""Choose the fixed capture gain for this microphone, from measurements of this microphone.

The ALSA control's label is not trustworthy. 'Mic Capture Volume' advertises 0..23.81 dB over
16 steps, i.e. 1.49 dB/step, but a two-point probe on the attached adapter measured far less:
the control moves the captured level, just not by the number printed on it. So the dB/step is
measured here rather than assumed, and the recommendation is derived from the speaker's own
voice instead of from room noise.

Procedure: put the microphone where it will work, run this, and speak normally while it
records. It reports a speech level taken from the 90th percentile of 100 ms frames (robust to
the gaps between words), the headroom the peaks leave, and the gain that hits the target.
Nothing changes unless --apply is given.
"""
import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave

from audio_device import capture_gain, resolve, set_capture_gain

RATE = 48000
TARGET_SPEECH_RMS_DBFS = -20.0   # leaves ~14 dB of peak headroom for a normal voice
MIN_HEADROOM_DB = 6.0


def record(dev, seconds, gain=None):
    """Record one window, optionally setting the capture gain first."""
    if gain is not None:
        set_capture_gain(dev, gain)
    handle, path = tempfile.mkstemp(suffix='.wav')
    os.close(handle)
    args = dev.arecord_args(seconds) + ['-t', 'wav', path]
    try:
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        pass
    return path


def measure(path):
    w = wave.open(path)
    frames = w.getnframes()
    samples = struct.unpack('<%dh' % frames, w.readframes(frames))
    frame = RATE // 10                                   # 100 ms
    levels = []
    for start in range(0, frames - frame + 1, frame):
        block = samples[start:start + frame]
        levels.append(math.sqrt(sum(x * x for x in block) / len(block)))
    levels.sort()
    peak = max(abs(x) for x in samples) or 1
    speech = levels[int(len(levels) * 0.9)] or 1
    floor = levels[len(levels) // 5] or 1
    db = lambda v: 20 * math.log10(v / 32768.0)          # noqa: E731
    return dict(peak_db=db(peak), speech_db=db(speech), floor_db=db(floor))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=6.0)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--target', type=float, default=TARGET_SPEECH_RMS_DBFS)
    args = ap.parse_args()

    dev = resolve()
    print(dev.describe())
    gain_now = capture_gain(dev)
    print('当前增益: %s' % gain_now)

    print('\n[1/3] 两点探测，测出这个声卡实际的 dB/步 ...')
    p0 = measure(record(dev, 1.5, gain=0))
    p16 = measure(record(dev, 1.5, gain=16))
    per_step = (p16['speech_db'] - p0['speech_db']) / 16.0
    print('    gain 0: %.1f dBFS | gain 16: %.1f dBFS | 实测 %.3f dB/步 (标称 1.49)'
          % (p0['speech_db'], p16['speech_db'], per_step))

    print('\n[2/3] 恢复原增益 %s，请在这 %.0f 秒内正常说一句话 ...'
          % (gain_now, args.seconds))
    if gain_now is not None:
        set_capture_gain(dev, gain_now)
    m = measure(record(dev, args.seconds))
    print('    peak %.1f dBFS | 语音(90分位) %.1f dBFS | 底噪 %.1f dBFS'
          % (m['peak_db'], m['speech_db'], m['floor_db']))
    snr = m['speech_db'] - m['floor_db']
    print('    语音-底噪 %.1f dB %s'
          % (snr, '(偏低，检查麦克风位置或指向)' if snr < 20 else ''))

    print('\n[3/3] 建议')
    if per_step <= 0.05:
        print('    这个声卡的音量控件几乎没有作用(%.3f dB/步)，增益不是可调项。' % per_step)
        return 0
    steps = int(round((args.target - m['speech_db']) / per_step))
    new_gain = max(0, min(16, gain_now + steps)) if gain_now is not None else None
    if new_gain is None:
        print('    该卡没有音量控件，无法调整。')
        return 0
    delta = (new_gain - gain_now) * per_step
    projected_peak = m['peak_db'] + delta
    print('    目标语音 %.0f dBFS -> 增益 %d (预计 %+.1f dB)，峰值约 %.1f dBFS'
          % (args.target, new_gain, delta, projected_peak))
    if projected_peak > -MIN_HEADROOM_DB:
        new_gain = max(0, new_gain - 1)
        print('    ⚠ 峰值余量不足，降一档 -> 增益 %d' % new_gain)
    if args.apply and new_gain != gain_now:
        set_capture_gain(dev, new_gain)
        print('    已应用: 增益 %d (用 MM_CAPTURE_GAIN=%d 固化到 setup_capture.sh)'
              % (new_gain, new_gain))
    elif not args.apply:
        print('    (未改动。加 --apply 应用，或 MM_CAPTURE_GAIN=%d bash setup_capture.sh)'
              % new_gain)
    return 0


if __name__ == '__main__':
    sys.exit(main())
