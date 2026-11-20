"""Produce a human-editable draft annotation for a calibration recording.

Splits the wav with the SAME VAD configuration the live worker uses (Silero,
0.4 s silence, x4 pre-gain), transcribes each segment with SenseVoice, and also
reports the pauses *inside* each segment plus the gap to the next one. The result
is a CSV/Markdown skeleton: nobody should pretend the ASR text is ground truth,
so the human-editable columns are left blank.
"""
import csv
import json
import sys

import numpy as np
import sherpa_onnx
import wave

ROOT = '/root/mm_audio_v1'
SR = 16000
GAIN = 4.0


def load(path):
    with wave.open(path) as handle:
        assert handle.getframerate() == SR and handle.getnchannels() == 1
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2').astype(np.float32) / 32768.0


def build_vad():
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = ROOT + '/models/silero_vad.onnx'
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.4
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 12
    cfg.sample_rate = SR
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)


def build_asr():
    model = ROOT + '/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model + '/model.int8.onnx', tokens=model + '/tokens.txt',
        language='zh', use_itn=True)


def intra_pauses(segment, threshold_db=-38.0, min_gap_ms=120):
    """Silences inside one VAD segment: the thing a 0.4 s rule can split."""
    frame = int(0.02 * SR)
    if len(segment) < frame * 2:
        return []
    frames = len(segment) // frame
    rms = np.array([np.sqrt(np.mean(segment[i * frame:(i + 1) * frame] ** 2) + 1e-12)
                    for i in range(frames)])
    db = 20 * np.log10(rms + 1e-12)
    quiet = db < threshold_db
    gaps, start = [], None
    for index, value in enumerate(quiet):
        if value and start is None:
            start = index
        elif not value and start is not None:
            length = (index - start) * 20
            if length >= min_gap_ms:
                gaps.append(length)
            start = None
    return gaps


def main(path):
    audio = load(path)
    vad = build_vad()
    asr = build_asr()
    window = vad.config.silero_vad.window_size

    blocks = []
    for offset in range(0, len(audio), window):
        chunk = np.clip(audio[offset:offset + window] * GAIN, -1.0, 1.0)
        vad.accept_waveform(chunk)

    while not vad.empty():
        seg = np.asarray(vad.front.samples, dtype=np.float32) / GAIN
        start = vad.front.start
        vad.pop()
        if len(seg) < 4000:
            continue
        stream = asr.create_stream()
        stream.accept_waveform(SR, np.clip(seg, -1.0, 1.0))
        asr.decode_stream(stream)
        blocks.append(dict(start=start, end=start + len(seg), text=stream.result.text,
                           audio=seg))

    rows = []
    for index, block in enumerate(blocks):
        nxt = blocks[index + 1]['start'] if index + 1 < len(blocks) else None
        rows.append(dict(
            idx=index + 1,
            start_s=round(block['start'] / SR, 2),
            end_s=round(block['end'] / SR, 2),
            dur_s=round((block['end'] - block['start']) / SR, 2),
            intra_pauses_ms=';'.join(str(g) for g in intra_pauses(block['audio'])) or '-',
            gap_to_next_s=(round((nxt - block['end']) / SR, 2) if nxt else ''),
            draft_text=block['text'],
            corrected_text='',
            boundary_ok='',
            pause_type='',
            notes='',
        ))

    base = path.rsplit('/', 1)[-1].replace('.wav', '')
    csv_path = '%s/results/annotation_%s.csv' % (ROOT, base)
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    json_path = '%s/results/annotation_%s.json' % (ROOT, base)
    json.dump(rows, open(json_path, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    print('wrote', csv_path)


if __name__ == '__main__':
    main(sys.argv[1])
