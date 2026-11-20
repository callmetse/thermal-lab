#!/usr/bin/env python3
"""Self-test: does the KWS model + our keywords file actually fire?

Runs the model over the archive's own test wavs with the archive's own
keywords, so it proves the code path before any live microphone is involved.
"""
import inspect
import sys
import wave

import numpy as np
import sherpa_onnx

M = '/home/pi/mm_audio_v1/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01'
ENC = f'{M}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'
DEC = f'{M}/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'
JOI = f'{M}/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'

print('--- KeywordSpotter signature ---')
try:
    print(inspect.signature(sherpa_onnx.KeywordSpotter.__init__))
except Exception as exc:  # pragma: no cover
    print('signature unavailable:', exc)


def make_spotter(keywords_file):
    return sherpa_onnx.KeywordSpotter(
        tokens=f'{M}/tokens.txt',
        encoder=ENC, decoder=DEC, joiner=JOI,
        keywords_file=keywords_file,
        num_threads=2,
        keywords_score=1.0,
        keywords_threshold=0.25,
        max_active_paths=4,
        provider='cpu',
    )


def read_wav(path):
    with wave.open(path) as w:
        assert w.getnchannels() == 1
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768
    return samples, rate


def scan(spotter, samples, rate):
    stream = spotter.create_stream()
    stream.accept_waveform(rate, samples)
    stream.accept_waveform(rate, np.zeros(int(rate * 0.5), dtype=np.float32))  # tail padding
    stream.input_finished()
    hits = []
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        r = spotter.get_result(stream)
        if r:
            hits.append(r)
            spotter.reset_stream(stream)
    return hits


kw = f'{M}/test_wavs/test_keywords.txt'
sp = make_spotter(kw)
print('keywords loaded from', kw)

ok = 0
for i in range(7):
    path = f'{M}/test_wavs/{i}.wav'
    try:
        samples, rate = read_wav(path)
    except Exception as exc:
        print(f'{i}.wav  SKIP {exc}')
        continue
    hits = scan(sp, samples, rate)
    if hits:
        ok += 1
    print(f'{i}.wav  {rate}Hz {len(samples)/rate:.2f}s  hits={hits}')

print('RESULT: wavs with a detection =', ok, '/ 7')
sys.exit(0 if ok else 1)
