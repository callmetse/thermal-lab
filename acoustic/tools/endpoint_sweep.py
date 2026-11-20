"""Sweep the Smart Turn threshold over existing recordings.

Same in-process feed as endpoint_ab.py, but runs several thresholds and both
recordings, and scores the outcome instead of dumping raw rows. Runs faster than
real time: without the 2 s idle gaps the idle-flush rule never fires, which is
acceptable here because we are comparing segmentation, not display latency.
"""
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGETS = ['我有点冷', '我有点热', '我不冷', '刚才有点冷']
CLEAN = 8          # a clean sentence transcript should be at least this long
GLUED = 14         # beyond this length it is probably two utterances joined


def load(path):
    with wave.open(path) as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2')


def run(mode, wav_path, threshold=None, silence=None, tail_seconds=2.0):
    os.environ['MM_ENDPOINT_MODE'] = mode
    for name in ('MM_ENDPOINT_THRESHOLD', 'MM_VAD_SILENCE_S', 'MM_ENDPOINT_SILENCE_S'):
        os.environ.pop(name, None)
    if threshold is not None:
        os.environ['MM_ENDPOINT_THRESHOLD'] = str(threshold)
    if silence is not None:
        os.environ['MM_VAD_SILENCE_S' if mode == 'fixed' else 'MM_ENDPOINT_SILENCE_S'] = str(silence)
    import importlib
    import worker as worker_mod
    importlib.reload(worker_mod)
    w = worker_mod.Worker(connect_io=False)
    got = []
    w.emit = lambda kind, **values: got.append(dict(kind=kind, **values))

    pcm = load(wav_path)
    pcm = np.concatenate([pcm, np.zeros(int(16000 * tail_seconds), dtype='<i2')])
    chunk = 4000
    session = 'sweep-%s-%s' % (mode, threshold)
    for index in range(0, len(pcm), chunk):
        raw = pcm[index:index + chunk].tobytes()
        now_ns = 1000000 * index
        header = dict(session_id=session, seq=index // chunk, sample_rate=16000, format='S16_LE',
                      pi_end_mono_ns=now_ns, pi_start_mono_ns=now_ns - int(len(raw) / 32000 * 1e9),
                      time_basis='sweep_fast', parent_run_id=None, pi_wall_ms=index,
                      hardware_sample_rate=16000)
        w.process(header, raw, now_ns)
    w.vad.flush()
    w.drain_speech()
    w.flush_merge('session_end')

    texts = [g.get('text') or '' for g in got if g['kind'] == 'transcript']
    clean, glued, fragments = 0, 0, 0
    for target in TARGETS:
        for text in texts:
            if target in text:
                if len(text) >= GLUED:
                    glued += 1
                elif len(text) >= CLEAN:
                    clean += 1
    for text in texts:
        if len(text) < CLEAN and not any(t in text for t in TARGETS):
            fragments += 1
    stats = dict(w.stats)
    return dict(mode=mode, threshold=threshold, wav=os.path.basename(wav_path),
                transcripts=len(texts), clean_targets=clean, glued_targets=glued,
                fragments=fragments, texts=texts,
                endpoint=dict(checks=stats.get('endpoint_checks'), merged=stats.get('endpoint_merged'),
                              timeouts=stats.get('endpoint_timeouts')))


def main():
    wavs = sys.argv[1:] or ['/root/mm_audio_v1/data/replay_30.wav',
                            '/root/mm_audio_v1/data/replay_139.wav']
    rows = []
    for wav in wavs:
        fixed = run('fixed', wav)
        rows.append(dict(fixed, threshold=None))
        print('fixed  %-16s transcripts=%d clean=%d glued=%d frag=%d'
              % (fixed['wav'], fixed['transcripts'], fixed['clean_targets'],
                 fixed['glued_targets'], fixed['fragments']), flush=True)
        for silence in (0.50, 0.40, 0.30):
            r = run('fixed', wav, silence=silence)
            r['silence'] = silence
            rows.append(r)
            print('fixed  %-16s silence=%.2f transcripts=%d clean=%d glued=%d frag=%d'
                  % (r['wav'], silence, r['transcripts'], r['clean_targets'],
                     r['glued_targets'], r['fragments']), flush=True)
        for threshold in (0.80, 0.85, 0.90, 0.95, 0.99):
            r = run('smart', wav, threshold=threshold)
            r['threshold'] = threshold
            rows.append(r)
            print('smart  %-16s thr=%.2f transcripts=%d clean=%d glued=%d frag=%d merged=%s'
                  % (r['wav'], threshold, r['transcripts'], r['clean_targets'],
                     r['glued_targets'], r['fragments'], r['endpoint']['merged']), flush=True)
    open('/root/mm_audio_v1/results/endpoint_sweep.json', 'w').write(
        json.dumps(rows, ensure_ascii=False, indent=1))
    print('\nwrote results/endpoint_sweep.json')


if __name__ == '__main__':
    main()
