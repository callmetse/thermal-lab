"""Evaluate the open-source Smart Turn v3.2 endpoint model on our own recordings.

Runs on the GPU host (numpy + onnxruntime only; torch is not installed there and is
not needed — the checkpoint ships as ONNX and Whisper's log-mel front-end is
reimplemented here in numpy, following OpenAI Whisper's reference recipe).

Question answered: how soon after a speaker stops can "this utterance is finished"
be decided, compared with the 0.5 s fixed silence rule used today?
"""
import json
import sys
import wave

import numpy as np
import onnxruntime as ort

MODEL = '/root/mm_audio_v1/models/smart-turn/smart-turn-v3.2-cpu.onnx'
SR = 16000
N_FFT = 400
HOP = 160
N_MELS = 80
CHUNK = 8 * SR


def _mel_scale(f):
    f = np.asarray(f, dtype=float)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=float)
    f_sp = 200.0 / 3
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), m * f_sp)


def mel_filterbank():
    """80 Slaney-normalised triangular filters over 0-8 kHz (Whisper/librosa default)."""
    mels = np.linspace(_mel_scale(0.0), _mel_scale(8000.0), N_MELS + 2)
    hz = _mel_to_hz(mels)
    freqs = np.linspace(0, SR / 2, N_FFT // 2 + 1)
    weights = np.zeros((N_MELS, len(freqs)))
    for i in range(N_MELS):
        lower, centre, upper = hz[i], hz[i + 1], hz[i + 2]
        up = (freqs - lower) / max(centre - lower, 1e-10)
        down = (upper - freqs) / max(upper - centre, 1e-10)
        weights[i] = np.maximum(0.0, np.minimum(up, down))
        weights[i] *= 2.0 / (upper - lower)   # Slaney area normalisation
    return weights.astype(np.float32)


FILTERS = mel_filterbank()
WINDOW = np.hanning(N_FFT + 1)[:-1].astype(np.float32)   # periodic Hann, as torch.hann_window


def log_mel(audio):
    """[80, 800] Whisper log-mel for exactly 8 s of 16 kHz audio."""
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) < CHUNK:
        audio = np.concatenate([np.zeros(CHUNK - len(audio), np.float32), audio])
    audio = audio[-CHUNK:]
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode='reflect')
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    frames = frames[:800] * WINDOW
    spectrum = np.fft.rfft(frames, n=N_FFT, axis=1)
    power = (np.abs(spectrum) ** 2).T                      # [201, 800]
    mel = FILTERS @ power                                  # [80, 800]
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


def read_wav(path):
    with wave.open(path, 'rb') as handle:
        assert handle.getframerate() == SR, handle.getframerate()
        assert handle.getnchannels() == 1
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main(path):
    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(MODEL, sess_options=options)
    audio = read_wav(path)

    # Energy-based speech activity, only to locate pause boundaries in this probe.
    frame = int(0.05 * SR)
    energy = np.array([np.sqrt(np.mean(audio[i:i + frame] ** 2) + 1e-12)
                       for i in range(0, len(audio) - frame, frame)])
    threshold = max(np.percentile(energy, 20) * 2.5,
                    0.02 * float(np.percentile(energy, 99)))
    voiced = energy > threshold

    # Pause onsets: voiced -> unvoiced transitions, kept only if the pause is real.
    ends = [i for i in range(1, len(voiced)) if voiced[i - 1] and not voiced[i]]

    def prob(at_seconds):
        end = int(at_seconds * SR)
        window = audio[max(0, end - CHUNK):end]
        feats = log_mel(window)[None, :, :]
        # The exported graph already ends in a sigmoid: its output IS the probability
        # (the repo's inference.py does `outputs[0][0].item()` with no sigmoid).
        return float(session.run(None, {'input_features': feats})[0].reshape(-1)[0])

    rows = []
    for index, end_frame in enumerate(ends):
        speech_end = end_frame * 0.05
        if speech_end < 1.0:
            continue
        # Only measure pauses that are genuinely followed by silence for a moment.
        after = voiced[end_frame:end_frame + 20]
        if after.any():
            continue
        rows.append({
            'speech_end_s': round(speech_end, 2),
            'p_at_0ms': round(prob(speech_end), 3),
            'p_at_100ms': round(prob(speech_end + 0.1), 3),
            'p_at_200ms': round(prob(speech_end + 0.2), 3),
            'p_at_300ms': round(prob(speech_end + 0.3), 3),
            'p_at_500ms': round(prob(speech_end + 0.5), 3),
        })
        print(rows[-1], flush=True)

    # Complement: windows cut in the middle of speech must look incomplete.
    mid = []
    for start in range(0, len(voiced) - 60, 100):
        if voiced[start:start + 60].all():
            t = (start + 2.0) * 0.05
            if t < len(audio) / SR - 1:
                mid.append(round(prob(t), 3))
    summary = {
        'file': path,
        'pauses': len(rows),
        'mean_p_by_delay': {k: round(float(np.mean([r[k] for r in rows])), 3) for k in
                            ('p_at_0ms', 'p_at_100ms', 'p_at_200ms', 'p_at_300ms', 'p_at_500ms')}
                            if rows else None,
        'mid_speech_mean_p': round(float(np.mean(mid)), 3) if mid else None,
        'mid_speech_samples': len(mid),
        'first_delay_reaching_0.5': next(
            (d for d in ('0ms', '100ms', '200ms', '300ms', '500ms')
             if rows and np.mean([r['p_at_' + d] for r in rows]) > 0.5), None),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    open('/root/mm_audio_v1/results/smart_turn_probe.json', 'w').write(
        json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/root/mm_audio_v1/data/probe.wav')
