"""Score sentence-boundary segmentation against the human annotation.

Runs the real Worker offline (no MQTT) over one wav under several endpoint
configurations and counts, per human-labelled sentence, how many output segments
it was cut into — the number the earlier "same transcript count" comparison
could not see.

Input: grouping.json (confirmed sentence groups + singletons) and the annotation
CSV for segment times. Output: JSON + a printable table.
"""
import csv
import io
import json
import os
import sys
import wave

import numpy as np

sys.path.insert(0, '/root/mm_audio_v1')
ROOT = '/root/mm_audio_v1'


def load_wav(path):
    with wave.open(path) as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2')


def run_config(wav_path, label, mode, threshold=None, silence=None, speaker_mode='single'):
    os.environ['MM_ENDPOINT_MODE'] = mode
    for name in ('MM_ENDPOINT_SILENCE_S', 'MM_ENDPOINT_THRESHOLD', 'MM_VAD_SILENCE_S'):
        os.environ.pop(name, None)
    if threshold is not None:
        os.environ['MM_ENDPOINT_THRESHOLD'] = str(threshold)
    if silence is not None:
        os.environ['MM_VAD_SILENCE_S'] = str(silence)
    import importlib
    import worker as worker_mod
    importlib.reload(worker_mod)
    w = worker_mod.Worker(connect_io=False)
    got = []
    w.emit = lambda kind, **values: got.append(dict(kind=kind, **values))

    pcm = load_wav(wav_path)
    pcm = np.concatenate([pcm, np.zeros(16000 * 2, dtype='<i2')])
    chunk = 4000
    for index in range(0, len(pcm), chunk):
        raw = pcm[index:index + chunk].tobytes()
        now_ns = 1_000_000 * index
        header = dict(session_id='score-%s' % label, seq=index // chunk, sample_rate=16000,
                      format='S16_LE', pi_end_mono_ns=now_ns,
                      pi_start_mono_ns=now_ns - int(len(raw) / 32000 * 1e9),
                      time_basis='offline_score', parent_run_id=None, pi_wall_ms=index,
                      speaker_mode=speaker_mode,
                      hardware_sample_rate=16000)
        w.process(header, raw, now_ns)
    w.vad.flush()
    w.drain_speech()
    w.flush_merge('session_end')

    outputs = []
    for item in got:
        if item['kind'] != 'transcript':
            continue
        start = (item.get('speech_start_sample') or 0) / 16000.0
        duration = item.get('speech_seconds') or 0
        outputs.append(dict(start=round(start, 2), end=round(start + duration, 2),
                            text=item.get('text') or ''))
    return dict(label=label, outputs=outputs)


def overlap(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def score(config, sentences):
    """sentences: [{name, start, end}] from the annotation."""
    per_sentence = []
    for sentence in sentences:
        hits = [o for o in config['outputs']
                if overlap(o['start'], o['end'], sentence['start'], sentence['end']) > 0]
        per_sentence.append(dict(name=sentence['name'], pieces=len(hits),
                                 texts=[h['text'] for h in hits]))
    split = sum(1 for item in per_sentence if item['pieces'] > 1)
    missing = sum(1 for item in per_sentence if item['pieces'] == 0)
    merged = 0
    for output in config['outputs']:
        covered = [s for s in sentences
                   if overlap(output['start'], output['end'], s['start'], s['end']) > 0]
        if len(covered) > 1:
            merged += 1
    return dict(label=config['label'], outputs=len(config['outputs']),
                sentences=len(sentences), split_sentences=split, missing_sentences=missing,
                merged_outputs=merged, per_sentence=per_sentence)


def main():
    annotation_csv = sys.argv[1]
    grouping_json = sys.argv[2]
    wav = sys.argv[3]

    rows = list(csv.DictReader(io.StringIO(open(annotation_csv, encoding='utf-8-sig').read())))
    times = {int(r['idx']): (float(r['start_s']), float(r['end_s']), r['draft_text']) for r in rows}
    grouping = json.loads(open(grouping_json, encoding='utf8').read())

    sentences = []
    used = set()
    for group in grouping['confirmed_groups']:
        first, last = min(group['segments']), max(group['segments'])
        sentences.append(dict(name=group['sentence'], start=times[first][0], end=times[last][1]))
        used.update(group['segments'])
    for idx in grouping['singletons']:
        if idx in used:
            continue
        text = grouping['corrected_text'].get(str(idx)) or times[idx][2]
        sentences.append(dict(name=text, start=times[idx][0], end=times[idx][1]))
    sentences.sort(key=lambda s: s['start'])

    specs = [('auto-single', 'auto', None, None, 'single'),
             ('auto-multi', 'auto', None, None, 'multi'),
             ('fixed-0.40', 'fixed', None, 0.40, 'single')]
    results = []
    for label, mode, threshold, silence, speaker in specs:
        config = run_config(wav, label, mode, threshold, silence, speaker)
        scored = score(config, sentences)
        results.append(scored)
        print('%-11s outputs=%2d split=%d missing=%d merged=%d'
              % (label, scored['outputs'], scored['split_sentences'],
                 scored['missing_sentences'], scored['merged_outputs']), flush=True)

    out = dict(annotation=annotation_csv, sentences=sentences, results=results)
    open(ROOT + '/results/boundary_score.json', 'w').write(json.dumps(out, ensure_ascii=False, indent=1))
    print('\nper sentence (fixed-0.40):')
    for item in results[0]['per_sentence']:
        print('  %-22s pieces=%d %s' % (item['name'][:20], item['pieces'], item['texts']))
    print('wrote results/boundary_score.json')


if __name__ == '__main__':
    main()
