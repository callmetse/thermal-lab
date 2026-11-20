"""A/B the endpoint rule offline: feed a wav straight through the real Worker code.

No MQTT, no queue, no timing noise from the network: this compares only what the
endpoint change does to (a) how many transcripts come out and (b) how long after
the true end of speech each one appears.
"""
import json
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(path):
    with wave.open(path) as handle:
        assert handle.getframerate() == 16000 and handle.getnchannels() == 1
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2')


def speech_end_seconds(samples):
    """Last moment of clearly voiced audio, by RMS (used only to anchor the timing)."""
    audio = samples.astype(np.float32) / 32768
    frame = 1600
    rms = np.array([np.sqrt(np.mean(audio[i:i + frame] ** 2) + 1e-12)
                    for i in range(0, len(audio) - frame, frame)])
    threshold = max(np.percentile(rms, 20) * 2.5, 0.02 * float(np.percentile(rms, 99)))
    voiced = np.where(rms > threshold)[0]
    return (voiced[-1] + 1) * 0.1 if len(voiced) else 0.0


def run(mode, wav_path, tail_seconds=2.5, silence=None):
    os.environ['MM_ENDPOINT_MODE'] = mode
    for name in ('MM_ENDPOINT_SILENCE_S', 'MM_ENDPOINT_THRESHOLD'):
        os.environ.pop(name, None)
    if silence is None:
        os.environ.pop('MM_VAD_SILENCE_S', None)
    else:
        os.environ['MM_VAD_SILENCE_S'] = str(silence)
    import importlib
    import worker as worker_mod
    importlib.reload(worker_mod)
    w = worker_mod.Worker(connect_io=False)
    got = []
    w.emit = lambda kind, **values: got.append(dict(kind=kind, at=time.monotonic(), **values))

    pcm = load(wav_path)
    pcm = np.concatenate([pcm, np.zeros(int(16000 * tail_seconds), dtype='<i2')])
    end = speech_end_seconds(pcm)
    chunk = 4000                      # 0.25 s
    started = time.monotonic()
    session = 'ab-%s' % mode
    for index in range(0, len(pcm), chunk):
        raw = pcm[index:index + chunk].tobytes()
        # Release after the block has elapsed, never feed future audio early.
        delay = started + min(index + chunk, len(pcm)) / 16000.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        now_ns = time.monotonic_ns()
        header = dict(session_id=session, seq=index // chunk, sample_rate=16000, format='S16_LE',
                      pi_end_mono_ns=now_ns, pi_start_mono_ns=now_ns - int(len(raw) / 32000 * 1e9),
                      time_basis='offline_ab', parent_run_id=None, pi_wall_ms=time.time() * 1000,
                      hardware_sample_rate=16000)
        w.process(header, raw, now_ns)
    w.vad.flush()
    w.drain_speech()
    w.flush_merge('session_end')

    transcripts = []
    for item in got:
        if item['kind'] != 'transcript':
            continue
        at = item['at'] - started
        transcripts.append(dict(text=item.get('text'), at=round(at - end, 2),
                                speech_seconds=round(item.get('speech_seconds') or 0, 2),
                                mode=item.get('endpoint_mode'), reason=item.get('endpoint_end_reason'),
                                merged=item.get('merged_segments'), p=item.get('endpoint_p')))
    stats = dict(w.stats)
    return dict(mode=mode, wav=os.path.basename(wav_path), speech_end_s=round(end, 2),
                timing_reference='RMS last-energy proxy; at is NOT per-utterance latency',
                io_mode='offline_no_mqtt',
                transcripts=transcripts, stats={k: stats.get(k) for k in
                                                ('endpoint_checks', 'endpoint_merged',
                                                 'endpoint_timeouts', 'asr_segments')})


def main():
    wav = sys.argv[1] if len(sys.argv) > 1 else '/root/mm_audio_v1/data/replay_30.wav'
    out = {}
    for label, mode, silence in (('fixed-0.50', 'fixed', 0.5), ('fixed-0.40', 'fixed', 0.4),
                                 ('fixed-0.30', 'fixed', 0.3), ('smart', 'smart', None)):
        result = run(mode, wav, silence=silence)
        out[label] = result
        print(json.dumps(result, ensure_ascii=False))
        print('---', label, 'done\n', flush=True)
    open('/root/mm_audio_v1/results/endpoint_ab.json', 'w').write(
        json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
