"""Feed a captured wav through the same Silero VAD config the worker uses.

Answers one question: does the VAD see speech in this file at all?
If it does here but the worker produced nothing, the fault is in the worker's
streaming loop, not in the audio or the model.
"""
import sys
import wave

import numpy as np
import sherpa_onnx as so

path = sys.argv[1]
with wave.open(path) as w:
    rate = w.getframerate()
    n = w.getnframes()
    samples = np.frombuffer(w.readframes(n), dtype='<i2').astype(np.float32) / 32768
print('file %s | %.2f s | %d Hz | mono' % (path, n / rate, rate))
print('peak %.3f  rms %.1f dBFS' % (float(np.max(np.abs(samples))),
                                    20 * float(np.log10(np.sqrt(np.mean(samples ** 2)) + 1e-12))))

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
print('window', window)

segments = []
for start in range(0, len(samples), window):
    chunk = samples[start:start + window]
    if len(chunk) == 0:
        break
    vad.accept_waveform(chunk)
    while not vad.empty():
        seg = np.asarray(vad.front.samples, dtype=np.float32).copy()
        segments.append((vad.front.start, len(seg)))
        vad.pop()
vad.flush()
while not vad.empty():
    seg = np.asarray(vad.front.samples, dtype=np.float32).copy()
    segments.append((vad.front.start, len(seg)))
    vad.pop()

print('segments: %d' % len(segments))
for start, ln in segments:
    print('  start_sample=%7d  start_s=%6.2f  dur_s=%5.2f' % (start, start / 16000, ln / 16000))

# Second opinion on the same file: SenseVoice over 4 s windows, to see whether the
# speech is intelligible at all even when the VAD stays silent.
asr = so.OfflineRecognizer.from_sense_voice(
    model='/root/mm_audio_v1/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx',
    tokens='/root/mm_audio_v1/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt',
    language='zh', use_itn=True, num_threads=2, provider='cpu')
print('--- fixed 4 s windows, ASR ---')
for i in range(0, max(1, len(samples)), 16000 * 4):
    chunk = samples[i:i + 16000 * 4]
    if len(chunk) < 8000:
        continue
    st = asr.create_stream()
    st.accept_waveform(16000, chunk)
    asr.decode_stream(st)
    text = st.result.text.strip()
    loud = 20 * float(np.log10(np.sqrt(np.mean(chunk ** 2)) + 1e-12))
    if text:
        print('  %5.1fs..%5.1fs rms=%.0f dB  %r' % (i / 16000, (i + len(chunk)) / 16000, loud, text))
