"""Existing pretrained sherpa-onnx models, run in an isolated cloud environment.

Design constraints kept explicit in the emitted events:
  * the microphone is a single mono channel at 48 kHz on the Pi; this process
    only ever sees 16 kHz mono S16_LE chunks that the Pi already resampled;
  * VAD segment start is reported in samples since the VAD was (re)created, not
    against any camera or ADC clock, so it must not be compared to video PTS;
  * audio-tagging scores are uncalibrated model outputs, and "no footstep
    detected" is not evidence that nobody walked;
  * counters are reported per session, because process-cumulative counters make
    a single capture round impossible to audit.
"""
import json
import os
import queue
import threading
import time
import numpy as np
import sherpa_onnx as so
from endpoint import EndpointDetector
from common import (ROOT, TOPIC, classify_audio_packet, event, journal, mqtt, publish,
                    split_candidates, unpack)


def blank_stats():
    return {'chunks': 0, 'tagger_runs': 0, 'published_results': 0, 'publish_failed': 0,
            'audio_seconds': 0.0,
            'asr_segments': 0, 'asr_infer_ms': 0.0, 'tag_infer_ms': 0.0,
            'queue_dropped': 0, 'gaps': 0,
            'endpoint_checks': 0, 'endpoint_merged': 0, 'endpoint_timeouts': 0}



# 唤醒词不该混进转写文本。只剥句首：句中出现可能是正常说话内容，不动。
WAKE_WORDS = [w.strip() for w in os.environ.get(
    'MM_WAKE_WORDS',
    '\u5c0fPI\u5c0fPI,\u5c0fpi\u5c0fpi,\u5c0f\u6d3e\u5c0f\u6d3e,\u5c0f\u76ae\u5c0f\u76ae,\u5c0fP\u5c0fP').split(',') if w.strip()]
_LEAD_PUNCT = ' \t\u3000\uff0c,\u3002.\u3001\uff01!\uff1f?\uff5e~\u201c\u201d"\''


def strip_wake_prefix(text):
    s = (text or '').strip()
    changed = True
    while changed:
        changed = False
        s = s.strip(_LEAD_PUNCT)
        low = s.lower()
        for w in WAKE_WORDS:
            if low.startswith(w.lower()):
                s = s[len(w):]
                changed = True
                break          # 立刻重算 low，否则会拿过期前缀再剥一遍
    return s.strip(_LEAD_PUNCT)


class Worker:
    def __init__(self, connect_io=True):
        self.queue = queue.Queue(maxsize=6)
        self.dropped_total = 0
        self.sid = None
        self.seq = -1
        self.vad_epoch = 0
        self.context = np.zeros(0, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.header = None
        self.vad = None
        self.stats = blank_stats()
        self.voice_seq = 0
        self.retired = {}
        self.last_chunk_mono_ns = None
        # Digital pre-gain in front of the VAD only. Measured on the 2026-11-03
        # quiet capture: the same audio yielded 0 segments at x1, 1 at x2 and 3
        # (covering the whole spoken region) at x4 with a 0.5 threshold. Raising
        # the threshold instead produced a false segment in the silent lead-in.
        # The ASR and the saved clips still get the original level.
        self.vad_gain = float(os.environ.get('MM_VAD_GAIN', '4.0'))
        self.vad_threshold = float(os.environ.get('MM_VAD_THRESHOLD', '0.5'))
        # Transport arrived as 2 s messages before; now it arrives as 0.25 s
        # messages so it does not queue behind vision traffic. Inference cadence
        # stays at TAGGING_INTERVAL_S samples so the compute cost is unchanged.
        self.tagging_interval = int(16000 * float(os.environ.get('MM_TAGGING_INTERVAL_S', '2.0')))
        self.accum = 0
        # Semantic end-of-turn (Smart Turn v3.2). Default stays 'fixed': the model
        # was measured on 6 sentence ends from one speaker only, and an early cut
        # mid-sentence is worse than 0.35 s of extra latency.
        # auto = follow the session's speaker_mode; fixed/smart force one behaviour.
        self.ep_mode = os.environ.get('MM_ENDPOINT_MODE', 'auto').strip().lower()
        self.ep_silence = float(os.environ.get('MM_ENDPOINT_SILENCE_S', '0.15'))
        self.ep_threshold = float(os.environ.get('MM_ENDPOINT_THRESHOLD', '0.9'))
        self.ep_max_merge = float(os.environ.get('MM_ENDPOINT_MAX_MERGE_S', '12'))
        # A held segment is only useful if the speaker continues; waiting 2 s
        # before giving up added ~2 s to every sentence that ended early.
        self.ep_idle_flush = float(os.environ.get('MM_ENDPOINT_IDLE_FLUSH_S', '0.6'))
        self.endpoint = None
        self.merge_buf = np.zeros(0, dtype=np.float32)
        self.merge_start = 0
        self.merge_parts = 0
        self.last_audio_mono_ns = None
        self.session_mode = 'single'
        self.smart_active = False
        if self.ep_mode in ('auto', 'smart'):
            self.endpoint = EndpointDetector()
            if not self.endpoint.available():
                print('endpoint: semantic model unavailable (%s); fixed silence only'
                      % self.endpoint.reason, flush=True)
                self.endpoint = None
                if self.ep_mode == 'smart':
                    self.ep_mode = 'fixed'
            else:
                print('endpoint: model loaded from %s (mode=%s)'
                      % (self.endpoint.path, self.ep_mode), flush=True)
        root = ROOT/'models'
        asr = root/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
        tag = root/'sherpa-onnx-zipformer-small-audio-tagging-2024-04-15'
        started = time.monotonic_ns()
        self.asr = so.OfflineRecognizer.from_sense_voice(
            model=str(asr/'model.int8.onnx'), tokens=str(asr/'tokens.txt'),
            language='zh', use_itn=True, num_threads=2, provider='cpu')
        cfg = so.AudioTaggingConfig()
        cfg.model.zipformer.model = str(tag/'model.int8.onnx')
        cfg.model.num_threads = 2
        cfg.labels = str(tag/'class_labels_indices.csv')
        cfg.top_k = 8
        self.tagger = so.AudioTagging(cfg)
        self.model_load_ms = (time.monotonic_ns()-started)/1e6
        # Offline evaluation must never subscribe to live audio or publish health.
        self.client = None
        if connect_io:
            self.client = mqtt('worker', self.message, [TOPIC+'/audio', TOPIC+'/commands'])
            threading.Thread(target=self.heartbeat, daemon=True).start()

    def resolve_endpoint(self, session_mode):
        """Should this session use the semantic merge? Pure decision, easy to test."""
        if self.endpoint is None:
            return False
        if self.ep_mode == 'smart':
            return True
        if self.ep_mode == 'fixed':
            return False
        return (session_mode or 'single') == 'single'

    def new_vad(self):
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str(ROOT/'models'/'silero_vad.onnx')
        cfg.silero_vad.threshold = self.vad_threshold
        # Fixed mode honours MM_VAD_SILENCE_S so the 0.5 s rule can be swept
        # without patching code; smart mode always uses its own shorter window.
        # 0.40 s measured: same segmentation as 0.50 on both test recordings, 0.24 s
        # faster to emit; 0.30 adds nothing further (0.89 -> 0.86 s).
        fixed_silence = float(os.environ.get('MM_VAD_SILENCE_S', '0.4'))
        cfg.silero_vad.min_silence_duration = (self.ep_silence
                                              if self.smart_active else fixed_silence)
        cfg.silero_vad.min_speech_duration = .25
        cfg.silero_vad.max_speech_duration = 12
        cfg.sample_rate = 16000
        cfg.num_threads = 1
        self.vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        self.vad_window = cfg.silero_vad.window_size
        self.vad_epoch += 1

    def heartbeat(self):
        while True:
            sa = self.stats['audio_seconds'] or 0.0
            age = (time.monotonic_ns()-self.last_chunk_mono_ns)/1e9 if self.last_chunk_mono_ns else None
            publish(self.client, 'health', {
                'status': 'ready',
                'session_id': self.sid,
                'last_chunk_age_s': None if age is None else round(age, 2),
                'models': ['sherpa-SenseVoice-20240717-int8', 'sherpa-zipformer-audioset-20240415-int8',
                           'silero-vad-onnx'],
                'provider': 'cpu', 'threads_per_model': 2,
                'model_load_ms': self.model_load_ms,
                'chunks': self.stats['chunks'],
                'published_results': self.stats['published_results'],
                'audio_seconds': round(sa, 3),
                'asr_segments': self.stats['asr_segments'],
                'asr_infer_ms': round(self.stats['asr_infer_ms'], 1),
                'tag_infer_ms': round(self.stats['tag_infer_ms'], 1),
                'tag_rtf': round(self.stats['tag_infer_ms']/1000/sa, 4) if sa > 0 else None,
                'queue_dropped_session': self.stats['queue_dropped'],
                'queue_dropped_total': self.dropped_total,
                'gaps': self.stats['gaps'],
                'tagger_runs': self.stats['tagger_runs'],
                'publish_failed': self.stats['publish_failed'],
                'tagging_interval_s': self.tagging_interval/16000,
                'vad_epoch': self.vad_epoch,
                'vad_gain': self.vad_gain,
                'vad_threshold': self.vad_threshold,
                'endpoint_mode': self.ep_mode,
                'fixed_silence_s': float(os.environ.get('MM_VAD_SILENCE_S', '0.4')),
                'time_basis': 'per_session_counters_reset_on_session_or_gap',
                'validation': 'unverified_scores_and_latency_not_benchmarked'})
            time.sleep(5)

    def message(self, client, userdata, msg):
        item = (msg.topic, bytes(msg.payload), time.monotonic_ns())
        try:
            self.queue.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.stats['queue_dropped'] += 1
        self.dropped_total += 1
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.stats['queue_dropped'] += 1
            self.dropped_total += 1

    def emit(self, kind, **values):
        x = event(kind, self.sid, source=self.header, **values)
        self.stats['published_results'] += 1
        journal('events.jsonl', x)
        # Right after a restart the client may not be connected yet; losing a marker
        # silently is worse than losing audio, so retry once and count failures.
        if self.client is not None and not publish(self.client, 'results', x, expiry=60):
            time.sleep(0.3)
            if not publish(self.client, 'results', x, expiry=60):
                self.stats['publish_failed'] += 1
        print(json.dumps({'kind': kind, 'session': self.sid,
                          'infer_ms': values.get('infer_ms'),
                          'text': values.get('text'), 'error': values.get('error')},
                         ensure_ascii=False), flush=True)

    def _transcribe(self, samples, start_sample, endpoint_p=None, merged=1, end_reason='vad'):
        if len(samples) < 4000:
            return
        path = ROOT/'data'/'speech'/self.sid
        path.mkdir(parents=True, exist_ok=True)
        file = path/('%04d.wav' % self.voice_seq)
        self.voice_seq += 1
        so.write_wave(str(file), samples, 16000)
        t = time.monotonic_ns()
        stream = self.asr.create_stream()
        stream.accept_waveform(16000, samples)
        self.asr.decode_stream(stream)
        text = stream.result.text
        infer_ms = (time.monotonic_ns()-t)/1e6
        self.stats['asr_segments'] += 1
        self.stats['asr_infer_ms'] += infer_ms
        # 唤醒词不进转写：只剥句首，剥完为空就整段丢弃（纯唤醒噪声）。
        text = strip_wake_prefix(text)
        if not text:
            self.stats['wake_only_segments'] = self.stats.get('wake_only_segments', 0) + 1
            return
        # One event per sentence: a short pause (<0.5 s) makes the VAD return one
        # clip holding several sentences, and a single whole-text candidate would
        # only reflect the last clause.
        parts = split_candidates(text)
        for index, part in enumerate(parts):
            self.emit('transcript', text=part['text'], candidate=part['candidate'],
                      segment_index=index, segment_count=len(parts),
                      speech_seconds=len(samples)/16000,
                      speech_start_sample=int(start_sample),
                      speech_time_basis='samples_since_vad_reset', vad_epoch=self.vad_epoch,
                      speech_file=str(file), infer_ms=infer_ms / len(parts),
                      asr_segment_infer_ms=infer_ms,
                      infer_ms_scope='amortized_across_sentence_events',
                      validation='unconfirmed_asr',
                      model='sherpa-SenseVoice-20240717-int8',
                      endpoint_mode=('smart' if self.smart_active else 'fixed'),
                      speaker_mode=self.session_mode, endpoint_p=endpoint_p,
                      endpoint_end_reason=end_reason, merged_segments=merged)

    def drain_speech(self):
        while not self.vad.empty():
            samples = np.clip(np.asarray(self.vad.front.samples, dtype=np.float32)
                              / max(self.vad_gain, 1e-6), -1.0, 1.0)
            start_sample = self.vad.front.start
            self.vad.pop()
            if len(samples) < 4000:
                continue
            if not self.smart_active:
                self._transcribe(samples, start_sample)
                continue
            # Smart mode: the VAD now cuts after only ep_silence seconds, so a pause
            # no longer ends the sentence by itself. Keep merging while the model says
            # the speaker is not finished; emit once it does (or when the cap is hit).
            joined = (np.concatenate([self.merge_buf, samples])
                      if len(self.merge_buf) else samples)
            p = self.endpoint.probability(joined)
            self.stats['endpoint_checks'] += 1
            done = p is not None and p >= self.ep_threshold
            too_long = len(joined) / 16000 >= self.ep_max_merge
            if done or too_long:
                first = self.merge_start if self.merge_parts else start_sample
                merged = self.merge_parts + 1
                self.merge_buf = np.zeros(0, dtype=np.float32)
                self.merge_start = 0
                self.merge_parts = 0
                self._transcribe(joined, first, endpoint_p=p, merged=merged,
                                 end_reason='threshold' if done else 'max_merge')
            else:
                if not self.merge_parts:
                    self.merge_start = start_sample
                self.merge_buf = joined
                self.merge_parts += 1
                self.stats['endpoint_merged'] += 1

    def flush_merge(self, reason='idle'):
        """Emit a held segment when nothing more arrives; silence must bound the wait."""
        if not len(self.merge_buf):
            return
        audio, first, merged = self.merge_buf, self.merge_start, self.merge_parts + 1
        self.merge_buf = np.zeros(0, dtype=np.float32)
        self.merge_start = 0
        self.merge_parts = 0
        self.stats['endpoint_timeouts'] += 1
        self._transcribe(audio, first, endpoint_p=None, merged=merged, end_reason=reason)

    def process(self, h, pcm, received):
        now_ns = time.monotonic_ns()
        if (self.smart_active and self.merge_parts
                and self.last_audio_mono_ns is not None
                and (now_ns - self.last_audio_mono_ns) / 1e9 > 2.0):
            self.flush_merge('idle')
        self.last_audio_mono_ns = now_ns
        queue_ms = (now_ns-received)/1e6
        kind = classify_audio_packet(self.sid, self.seq, h, self.retired)
        if kind in ('duplicate', 'retired'):
            if kind == 'retired':
                self.stats['queue_dropped'] += 1
            return
        changed = kind == 'new_session'
        gap = kind == 'gap'
        if changed or gap:
            self.header = h   # must be set before any marker is emitted
            self.sid = h['session_id']
            self.session_mode = (h.get('speaker_mode') or 'single').lower()
            self.smart_active = self.resolve_endpoint(self.session_mode)
            self.new_vad()
            self.context = np.zeros(0, dtype=np.float32)
            self.pending = np.zeros(0, dtype=np.float32)
            if changed:
                self.voice_seq = 0
                self.stats = blank_stats()
                if h['seq'] != 0:
                    # QoS0 audio published while this worker was down is gone, and a
                    # restarted worker cannot infer that from sequence numbers. Say it
                    # explicitly instead of pretending the session started here.
                    self.emit('audio_joined_midsession', first_seq=h['seq'],
                              note='worker joined a running session; earlier chunks were not processed')
            else:
                self.stats['gaps'] += 1
        self.header = h
        if gap:
            self.emit('audio_gap', previous_seq=self.seq, next_seq=h['seq'],
                      vad_epoch=self.vad_epoch,
                      note='VAD and sample counters restarted; speech_start_sample is not comparable across gaps')
        self.seq = h['seq']
        self.last_chunk_mono_ns = received
        data = np.frombuffer(pcm, dtype='<i2').astype(np.float32)/32768
        self.context = np.concatenate([self.context, data])[-64000:]
        self.accum += len(data)
        self.pending = np.concatenate([self.pending, data])
        while len(self.pending) >= self.vad_window:
            window = np.clip(self.pending[:self.vad_window] * self.vad_gain, -1.0, 1.0)
            self.vad.accept_waveform(window)
            self.pending = self.pending[self.vad_window:]
        self.drain_speech()
        if self.accum < self.tagging_interval:
            self.stats['chunks'] += 1
            self.stats['audio_seconds'] += len(data)/16000
            return
        window_seconds = self.accum/16000
        self.accum = 0
        t = time.monotonic_ns()
        stream = self.tagger.create_stream()
        stream.accept_waveform(16000, self.context)
        tags = self.tagger.compute(stream)
        infer_ms = (time.monotonic_ns()-t)/1e6
        self.stats['chunks'] += 1
        self.stats['audio_seconds'] += len(data)/16000
        self.stats['tag_infer_ms'] += infer_ms
        self.stats['tagger_runs'] += 1
        rms = float(np.sqrt(np.mean(data*data))) if len(data) else 0
        self.emit('sound_events', events=[dict(name=x.name, score=float(x.prob), index=int(x.index))
                                          for x in tags],
                  window_seconds=window_seconds,
                  context_seconds=len(self.context)/16000,
                  rms_dbfs=20*float(np.log10(rms)) if rms > 0 else None,
                  clipping_fraction=float(np.mean(np.abs(data) >= .999)),
                  model='sherpa-zipformer-small-audioset-20240415-int8',
                  infer_ms=infer_ms, worker_queue_ms=queue_ms,
                  chunk_index=self.stats['chunks'],
                  validation='unverified_scores_not_calibrated_probabilities',
                  silence_is_not_negative_evidence=True)

    def run(self):
        while True:
            try:
                topic, raw, received = self.queue.get(timeout=0.2)
            except queue.Empty:
                # Without a timeout the idle-flush rule only ran when the next packet
                # arrived, so a held segment could sit forever after the stream stopped.
                if (self.smart_active and self.merge_parts and self.last_audio_mono_ns
                        and (time.monotonic_ns() - self.last_audio_mono_ns) / 1e9 > self.ep_idle_flush):
                    self.flush_merge('idle')
                continue
            try:
                if topic.endswith('/commands'):
                    cmd = json.loads(raw)
                    if cmd.get('op') == 'flush' and cmd.get('session_id') == self.sid and self.vad:
                        self.vad.flush()
                        self.drain_speech()
                        self.flush_merge('session_end')
                        self.retired[cmd['session_id']] = time.monotonic_ns()
                        while len(self.retired) > 8:
                            self.retired.pop(next(iter(self.retired)))
                        self.emit('worker_session_flushed', session_stats=self.stats,
                                  vad_epoch=self.vad_epoch,
                                  note='late packets from this session are ignored')
                else:
                    h, pcm = unpack(raw)
                    self.process(h, pcm, received)
            except Exception as ex:
                self.emit('worker_error', error=str(ex))


if __name__ == '__main__':
    Worker().run()
