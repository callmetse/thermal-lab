"""Semantic end-of-turn detector (Smart Turn v3.2, BSD-2, ONNX).

Replaces the fixed "0.5 s of silence means the sentence ended" rule with a learned
judgement on the last 8 s of audio. The checkpoint is the int8 CPU export; the
Whisper log-mel front-end is reimplemented here in numpy because the GPU host has
no torch, and it was verified element-wise against the torch reference (max abs
difference 0.0000).

Graceful degradation is mandatory: if the model file or onnxruntime is missing,
`available()` returns False and the worker keeps the old fixed-silence behaviour.
"""
import os
from pathlib import Path

import numpy as np

SR = 16000
N_FFT = 400
HOP = 160
N_MELS = 80
CHUNK = 8 * SR
MODEL_PATH = os.environ.get('MM_ENDPOINT_MODEL',
                            '/root/mm_audio_v1/models/smart-turn/smart-turn-v3.2-cpu.onnx')


def _mel_scale(f):
    f = np.asarray(f, dtype=float)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(f >= min_log_hz,
                    min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=float)
    f_sp = 200.0 / 3
    min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
    min_log_mel = min_log_hz / f_sp
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), m * f_sp)


def _filterbank():
    hz = _mel_to_hz(np.linspace(_mel_scale(0.0), _mel_scale(8000.0), N_MELS + 2))
    freqs = np.linspace(0, SR / 2, N_FFT // 2 + 1)
    weights = np.zeros((N_MELS, len(freqs)))
    for i in range(N_MELS):
        lower, centre, upper = hz[i], hz[i + 1], hz[i + 2]
        up = (freqs - lower) / max(centre - lower, 1e-10)
        down = (upper - freqs) / max(upper - centre, 1e-10)
        weights[i] = np.maximum(0.0, np.minimum(up, down)) * (2.0 / (upper - lower))
    return weights.astype(np.float32)


FILTERS = _filterbank()
WINDOW = np.hanning(N_FFT + 1)[:-1].astype(np.float32)


def log_mel(audio):
    """[80, 800] Whisper log-mel for exactly 8 s at 16 kHz (zeros padded at the front)."""
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) < CHUNK:
        audio = np.concatenate([np.zeros(CHUNK - len(audio), np.float32), audio])
    audio = audio[-CHUNK:]
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode='reflect')
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP][:800] * WINDOW
    power = (np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2).T
    log = np.log10(np.maximum(FILTERS @ power, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


class EndpointDetector:
    def __init__(self, model_path=MODEL_PATH):
        self.path = Path(model_path)
        self.session = None
        self.reason = ''
        try:
            import onnxruntime as ort
            if not self.path.exists():
                self.reason = 'model missing: %s' % self.path
                return
            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 2
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self.session = ort.InferenceSession(str(self.path), sess_options=options)
            self.session.run(None, {'input_features': log_mel(np.zeros(CHUNK, np.float32))[None]})
        except Exception as ex:                      # noqa: BLE001 - degrade, never crash
            self.session = None
            self.reason = '%s: %s' % (type(ex).__name__, ex)

    def available(self):
        return self.session is not None

    def probability(self, audio):
        """P(the speaker has finished their turn), in [0, 1].

        The exported graph already ends in a sigmoid, so the raw output is the
        probability; applying another sigmoid is a known trap (0.99 -> 0.73).
        """
        if self.session is None:
            return None
        features = log_mel(audio)[None, :, :]
        return float(self.session.run(None, {'input_features': features})[0].reshape(-1)[0])
