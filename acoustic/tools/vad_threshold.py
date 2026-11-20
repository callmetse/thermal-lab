"""Sweep Silero VAD threshold over a real capture to quantify the sensitivity limit."""
import sys
import wave
import numpy as np
import sherpa_onnx as so

path = sys.argv[1]
with wave.open(path) as w:
    samples = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768

for threshold in (0.5, 0.4, 0.3, 0.2, 0.1):
    cfg = so.VadModelConfig()
    cfg.silero_vad.model = '/root/mm_audio_v1/models/silero_vad.onnx'
    cfg.silero_vad.threshold = threshold
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
    print('threshold %.1f -> %d segments: %s' % (
        threshold, len(segs),
        ', '.join('%.1fs/%.2fs' % (s / 16000, d / 16000) for s, d in segs)))
