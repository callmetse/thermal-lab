"""Prove the three pretrained models actually load and infer on the worker host.

Run ON the worker host with its isolated venv. Uses only the sample wav files
shipped inside the official model archives, so no microphone and no user data
are involved. Records cold-start load time, inference time and RTF, and prints
the SHA256 of every model file it touched.
"""
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx as so

ROOT = Path('/root/mm_audio_v1')
MODELS = ROOT / 'models'
ASR = MODELS / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
TAG = MODELS / 'sherpa-onnx-zipformer-small-audio-tagging-2024-04-15'
SILERO = MODELS / 'silero_vad.onnx'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def read_wav(path):
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1, 'expected mono sample'
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768
    return samples, rate


def runtime_info():
    import platform
    info = {'python': sys.version.split()[0], 'platform': platform.platform(),
            'sherpa_onnx': getattr(so, '__version__', 'unknown')}
    try:
        import sherpa_onnx_core
        info['sherpa_onnx_core'] = getattr(sherpa_onnx_core, '__version__', 'unknown')
    except Exception as ex:
        info['sherpa_onnx_core'] = 'unavailable: %s' % ex
    try:
        import onnxruntime
        info['onnxruntime'] = onnxruntime.__version__
    except Exception as ex:
        info['onnxruntime'] = 'unavailable: %s' % ex
    libdir = Path(so.__file__).resolve().parent
    libs = sorted(list(libdir.glob('*.so')) + list(libdir.glob('**/*.so')))[:6]
    info['libs'] = {str(x): sha256(x) for x in libs}
    return info


def main():
    report = {'host': 'worker', 'samples_only': True, 'models': {},
              'runtime': runtime_info()}
    print('runtime', json.dumps(report['runtime'], ensure_ascii=False), flush=True)
    for path in (ASR / 'model.int8.onnx', ASR / 'tokens.txt', TAG / 'model.int8.onnx',
                 TAG / 'class_labels_indices.csv', SILERO):
        st = path.stat()
        report['models'][str(path)] = {'bytes': st.st_size, 'sha256': sha256(path)}
        print('%12d %s %s' % (st.st_size, sha256(path)[:16], path), flush=True)

    t = time.time()
    asr = so.OfflineRecognizer.from_sense_voice(
        model=str(ASR / 'model.int8.onnx'), tokens=str(ASR / 'tokens.txt'),
        language='zh', use_itn=True, num_threads=2, provider='cpu')
    report['asr_load_s'] = round(time.time() - t, 2)
    print('SenseVoice loaded in %.2fs' % report['asr_load_s'], flush=True)

    t = time.time()
    cfg = so.AudioTaggingConfig()
    cfg.model.zipformer.model = str(TAG / 'model.int8.onnx')
    cfg.model.num_threads = 2
    cfg.labels = str(TAG / 'class_labels_indices.csv')
    cfg.top_k = 8
    tagger = so.AudioTagging(cfg)
    report['tagger_load_s'] = round(time.time() - t, 2)
    print('Audio tagging loaded in %.2fs' % report['tagger_load_s'], flush=True)

    t = time.time()
    vad_cfg = so.VadModelConfig()
    vad_cfg.silero_vad.model = str(SILERO)
    vad_cfg.sample_rate = 16000
    vad_cfg.num_threads = 1
    vad = so.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=30)
    report['vad_load_s'] = round(time.time() - t, 2)
    report['vad_window'] = int(vad_cfg.silero_vad.window_size)
    print('Silero VAD loaded in %.2fs (window=%d)' % (report['vad_load_s'], report['vad_window']),
          flush=True)

    report['asr'] = {}
    for wav in sorted((ASR / 'test_wavs').glob('*.wav')):
        samples, rate = read_wav(wav)
        if rate != 16000:
            raise SystemExit('%s is %d Hz; expected 16 kHz' % (wav, rate))
        t = time.time()
        stream = asr.create_stream()
        stream.accept_waveform(16000, samples)
        asr.decode_stream(stream)
        infer = time.time() - t
        audio_s = len(samples) / 16000
        report['asr'][wav.name] = {'text': stream.result.text, 'audio_s': round(audio_s, 2),
                                   'infer_ms': round(infer * 1000, 1),
                                   'rtf': round(infer / audio_s, 4)}
        print('ASR %-8s %6.2fs rtf=%.3f %r' % (wav.name, audio_s, infer / audio_s,
                                              stream.result.text), flush=True)

    report['tagging'] = {}
    for wav in sorted((TAG / 'test_wavs').glob('*.wav'))[:3]:
        samples, rate = read_wav(wav)
        t = time.time()
        stream = tagger.create_stream()
        stream.accept_waveform(rate, samples)
        tags = tagger.compute(stream)
        infer = time.time() - t
        report['tagging'][wav.name] = {'audio_s': round(len(samples) / rate, 2),
                                       'infer_ms': round(infer * 1000, 1),
                                       'top': [{'name': x.name, 'score': round(float(x.prob), 3)}
                                               for x in tags]}
        print('TAG %-8s %s' % (wav.name, [(x.name, round(float(x.prob), 2)) for x in tags[:3]]),
              flush=True)

    # VAD on the same sample wav: proves the detector runs, not that it is accurate.
    samples, _ = read_wav(ASR / 'test_wavs' / 'zh.wav')
    t = time.time()
    feeds = 0
    segments = 0
    for start in range(0, len(samples), report['vad_window']):
        chunk = samples[start:start + report['vad_window']]
        if len(chunk) == 0:
            break
        vad.accept_waveform(chunk)
        feeds += 1
        while not vad.empty():
            segments += 1
            vad.pop()
    vad.flush()
    while not vad.empty():
        segments += 1
        vad.pop()
    report['vad'] = {'feeds': feeds, 'segments_on_zh_sample': segments,
                     'wall_ms': round((time.time() - t) * 1000, 1),
                     'note': 'detector runs on the sample; accuracy not evaluated'}
    print('VAD segments on zh.wav: %d' % segments, flush=True)

    out = ROOT / 'results' / 'model_verification.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
    print('wrote', out, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
