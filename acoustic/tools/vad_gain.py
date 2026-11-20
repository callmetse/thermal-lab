"""Does a digital pre-gain recover VAD detection on the quiet capture?

If yes, the fix is a software gain in front of the VAD (no device change needed),
provided we clip-guard and still hand the *original* samples to the ASR.
"""
import sys
import wave
import numpy as np
import sherpa_onnx as so

path = sys.argv[1]
with wave.open(path) as w:
    raw = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768

for gain in (1.0, 2.0, 4.0, 8.0):
    samples = np.clip(raw * gain, -1.0, 1.0)
    peak = float(np.max(np.abs(samples)))
    clipped = float(np.mean(np.abs(raw * gain) > 1.0))
    cfg = so.VadModelConfig()
    cfg.silero_vad.model = '/root/mm_audio_v1/models/silero_vad.onnx'
    cfg.silero_vad.threshold = .5
    cfg.silero_vad.min_silence_duration = .5
    cfg.silero_vad.min_speech_duration = .25
    cfg.silero_vad.max_speech_duration = 12
    cfg.sample_rate = 16000
    cfg.num_threads = 1
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
    window = cfg.silero_vad.window_size
    segs = []
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window]
        if len(chunk) == 0:
            break
        vad.accept_waveform(chunk)
        while not vad.empty():
            segs.append((vad.front.start, len(np.asarray(vad.front.samples))))
            vad.pop()
    vad.flush()
    while not vad.empty():
        segs.append((vad.front.start, len(np.asarray(vad.front.samples))))
        vad.pop()
    print('gain x%.0f (peak %.2f, would-clip %.3f%%) -> %d segs: %s' % (
        gain, peak, clipped * 100, len(segs),
        ', '.join('%.1fs/%.2fs' % (s / 16000, d / 16000) for s, d in segs)))
