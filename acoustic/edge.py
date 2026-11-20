"""Pi recorder + bounded transport + authenticated reverse-proxy-only UI backend."""
import asyncio
import collections
import gzip
import json
import logging
import logging.handlers
import math
import os
import subprocess
import threading
import time
import uuid
import wave
from aiohttp import web
import merge
from common import (ROOT, TOPIC, ConfirmationError, confirm_transcript, event, fuse,
                    journal, mqtt, pack, publish)
from audio_device import warm as warm_capture, resolve as resolve_capture


CHUNK_BYTES = 8000  # 0.25 s of 16 kHz S16_LE


class Edge:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.active = None
        self.last_session = None
        self.status = 'idle'
        self.error = None
        self.thread = None
        self.events = collections.deque(maxlen=120)
        self.visual = None
        self.visual_history = collections.deque(maxlen=180)
        self.stats = {}
        self.worker = None
        self.procs = None
        self.confirmed = {}
        # Two connections, mirroring the ABC split that was measured to keep
        # latency low: audio is bulk and loss-tolerant (QoS0, own connection),
        # results/health/observations use QoS1.
        self.audio_client = mqtt('edge-audio', lambda *a: None, [])
        self.client = mqtt('edge-results', self.message, [TOPIC+'/results', TOPIC+'/health',
                           'labs/abc20261102/v1/runs/+/observations'])

    def message(self, client, userdata, msg):
        try:
            data = gzip.decompress(msg.payload) if msg.payload[:2] == b'\x1f\x8b' else msg.payload
            if len(data) > 8*1024*1024:
                return
            x = json.loads(data)
            # A result may carry source=None (markers emitted before a header was
            # known); normalise it so one bad field cannot swallow the event.
            if not isinstance(x.get('source'), dict):
                x['source'] = {}
            now = time.monotonic_ns()
            with self.lock:
                if msg.topic.endswith('/health'):
                    self.worker = dict(x, received_mono_ns=now)
                    return
                if msg.topic.startswith('labs/abc20261102/'):
                    self.visual = dict(x, pi_received_mono_ns=now)
                    self.visual_history.append(self.visual)
                    return
                if x.get('session_id') != self.last_session:
                    journal('late_results.jsonl', x)
                    return
                if any(e.get('event_id') == x.get('event_id') for e in self.events):
                    return
                if x.get('source', {}).get('pi_end_mono_ns'):
                    x['pi_chunk_to_result_ms'] = (now-x['source']['pi_end_mono_ns'])/1e6
                h = x.get('source', {}) or {}
                v = next((v for v in reversed(self.visual_history)
                          if v.get('run_id') == h.get('parent_run_id')
                          and v['pi_received_mono_ns'] <= h.get('pi_end_mono_ns', 0)), None) \
                    if h.get('parent_run_id') else None
                if x.get('kind') == 'sound_events':
                    x['fusion'] = fuse(x, v)
                elif x.get('kind') == 'transcript':
                    # A transcript is itself speech evidence for the same run window.
                    x['fusion'] = fuse(dict(x, events=[{'name': 'Speech', 'score': 1.0}]), v)
                x['pi_result_received_mono_ns'] = now
                self.events.appendleft(x)
                journal('events.jsonl', x)
        except Exception as ex:
            journal('errors.jsonl', event('receive_error', self.last_session, error=str(ex)))

    def capture(self, sid, seconds, parent_run_id, speaker_mode='single'):
        path = ROOT/'data'/'audio'/sid
        path.mkdir(parents=True, exist_ok=True)
        chunks = sent = rejected = 0
        p = ff = None
        try:
            with (path/'capture.log').open('wb') as err, wave.open(str(path/'capture_16k.wav'), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                dev = resolve_capture()
                err.write((dev.describe() + '\n').encode()); err.flush()
                p = subprocess.Popen(dev.arecord_args(seconds), stdout=subprocess.PIPE, stderr=err)
                ff = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                     '-probesize', '32', '-analyzeduration', '0', '-f', 's16le',
                     '-ar', str(dev.rate), '-ac', str(dev.channels),
                     '-i', 'pipe:0', '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1'],
                     stdin=p.stdout, stdout=subprocess.PIPE, stderr=err)
                p.stdout.close()
                with self.lock:
                    self.procs = (p, ff)
                while not self.stop.is_set():
                    # 0.25 s per MQTT message. Large 64 KB messages sat behind the
                    # ABC vision stream (~360 KB/s of JPEGs on the same tunnel) and
                    # cost hundreds of ms of head-of-line waiting; the ABC transport
                    # lesson is to keep bulk traffic in small, loss-tolerant packets.
                    raw = ff.stdout.read(CHUNK_BYTES)
                    if not raw:
                        break
                    end = time.monotonic_ns()
                    wav.writeframesraw(raw)
                    h = dict(session_id=sid, seq=chunks, sample_rate=16000, format='S16_LE',
                             speaker_mode=speaker_mode,
                             pi_end_mono_ns=end, pi_start_mono_ns=end-int(len(raw)/32000*1e9),
                             time_basis='resampled_pipe_read_complete_not_adc_timestamp',
                             parent_run_id=parent_run_id, pi_wall_ms=time.time()*1000,
                             hardware_sample_rate=dev.rate)
                    okay = publish(self.audio_client, 'audio', pack(h, raw), expiry=8, qos=0)
                    sent += int(okay); rejected += int(not okay); chunks += 1
                    with self.lock:
                        self.stats = dict(chunks=chunks, publish_accepted=sent,
                                          publish_rejected=rejected, audio_seconds=wav.getnframes()/16000,
                                          capture_path=str(path/'capture_16k.wav'))
                # Normal EOF must have successful capture and resampling exit status.
                if not self.stop.is_set():
                    if ff.wait(timeout=5) or p.wait(timeout=5):
                        raise RuntimeError('capture or resampling failed; inspect capture.log')
        except Exception as ex:
            self.error = str(ex)
        finally:
            # Tell the worker to flush this session exactly once, even after a failure,
            # so a later session never inherits a half-open VAD segment.
            try:
                publish(self.client, 'commands', {'op':'flush', 'session_id':sid}, expiry=30)
            except Exception:
                pass
            for proc in (ff, p):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            with self.lock:
                self.procs = None
                self.active = None
                self.status = 'failed' if self.error else 'finished'
                journal('sessions.jsonl', event('capture_finished', sid, stats=self.stats, error=self.error))

    async def start(self, request):
        body = await request.json()
        seconds = body.get('seconds', 60)
        if type(seconds) is not int or not 4 <= seconds <= 180:
            raise web.HTTPBadRequest(text='seconds must be an integer from 4 to 180')
        # single = one speaker in the room (semantic merge allowed);
        # multi = somebody else may talk too (fixed 0.4 s, never merge).
        speaker_mode = str(body.get('speaker_mode') or 'single').lower()
        if speaker_mode not in ('single', 'multi'):
            raise web.HTTPBadRequest(text='speaker_mode must be single or multi')
        with self.lock:
            if self.active or (self.thread and self.thread.is_alive()):
                raise web.HTTPConflict(text='capture already running')
            if not self.client.is_connected() or not self.audio_client.is_connected():
                raise web.HTTPServiceUnavailable(text='MQTT disconnected')
            if not self.worker or time.monotonic_ns()-self.worker['received_mono_ns'] > 15e9:
                raise web.HTTPServiceUnavailable(text='audio worker not ready')
            self.active = self.last_session = uuid.uuid4().hex
            self.status = 'recording'; self.error = None; self.stats = {}; self.events.clear()
            self.confirmed.clear()
            self.stop.clear()
            sid = self.active
            self.thread = threading.Thread(target=self.capture,
                args=(sid, seconds, body.get('parent_run_id'), speaker_mode), daemon=True)
            self.thread.start()
            journal('sessions.jsonl', event('capture_started', sid, seconds=seconds,
                                            parent_run_id=body.get('parent_run_id')))
        return web.json_response({'session_id':sid, 'status':'recording', 'seconds':seconds})

    async def stop_capture(self, request):
        self.stop.set()
        # Unblock the recorder immediately: killing the resampler ends the read loop
        # instead of waiting up to one read (about two seconds) for more audio.
        with self.lock:
            procs = self.procs
        for proc in procs or ():
            if proc.poll() is None:
                proc.terminate()
        return web.json_response({'status':'stopping'})

    def snapshot(self):
        """One state payload, shared by the JSON endpoint and the push stream."""
        with self.lock:
            now = time.monotonic_ns()
            v = self.visual
            newest = next((e for e in self.events if e.get('pi_result_received_mono_ns')), None)
            return dict(status=self.status, active=self.active, session_id=self.last_session,
                        error=self.error, stats=self.stats, events=list(self.events),
                        confirmed_count=len(self.confirmed),
                        newest_result_age_s=(now-newest['pi_result_received_mono_ns'])/1e9
                        if newest else None,
                        # How long the newest result had already been waiting on this host
                        # when this snapshot was taken. It is the measurable part of the
                        # "speech end -> screen" delay that page refresh policy controls.
                        newest_result_wait_ms=(now-newest['pi_result_received_mono_ns'])/1e6
                        if newest else None,
                        server_mono_ns=now,
                        mqtt_connected=self.client.is_connected(), worker=self.worker,
                        worker_age_s=(now-self.worker['received_mono_ns'])/1e9 if self.worker else None,
                        visual={'run_id':v.get('run_id'), 'frame':v.get('source_frame_id'),
                                'people':len(v.get('tracks', [])),
                                'age_s':(now-v['pi_received_mono_ns'])/1e9} if v else None)

    async def state(self, request):
        return web.json_response(self.snapshot())

    async def feedback_state(self, request):
        """Read-only projection of confirmed feedback (review reply 方向3).

        Built from this host's own JSONL journal, refreshed only when the journal
        changed. Nothing here writes back: events stay the source of truth.
        """
        import feedback_state
        journal = ROOT/'data'/'events.jsonl'
        db_path = ROOT/'results'/'feedback_projection.sqlite'
        if not journal.exists():
            return web.json_response({'projection_version': feedback_state.SCHEMA_VERSION,
                                      'dimensions': {}, 'note': 'no journal yet'})
        stamp = journal.stat().st_mtime_ns
        cached = getattr(self, '_feedback_stamp', None)
        with self.lock:
            need_rebuild = cached != stamp
            if need_rebuild:
                self._feedback_stamp = stamp
        db = feedback_state.connect(str(db_path))
        try:
            if need_rebuild:
                feedback_state.ingest_journal(db, str(journal))
            dimensions = feedback_state.current(
                db, subject_id=request.query.get('subject') or None,
                zone_id=request.query.get('zone') or 'room-1')
        finally:
            db.close()
        return web.json_response({
            'projection_version': feedback_state.SCHEMA_VERSION,
            'display_policy': feedback_state.DISPLAY_POLICY,
            'display_ttl_s': feedback_state.DEFAULT_TTL_S,
            'subject_key': request.query.get('subject') or 'unknown_speaker',
            'journal_mtime_ns': stamp,
            'dimensions': dimensions,
        })

    async def stream(self, request):
        """Server-sent events: push state as soon as it changes.

        Replaces the 1 s polling loop that added 0-1000 ms of pure display lag.
        A heartbeat is pushed every 3 s so the page can still tell "no news" from
        "server gone".
        """
        response = web.StreamResponse(headers={
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        })
        await response.prepare(request)
        last_key, last_push = None, 0.0
        try:
            while True:
                data = self.snapshot()
                key = (data['session_id'], data['status'], data['active'], len(data['events']),
                       data['events'][0].get('event_id') if data['events'] else None,
                       data['confirmed_count'], data['error'])
                now = time.monotonic()
                if key != last_key or now - last_push > 3.0:
                    last_key, last_push = key, now
                    frame = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                    await response.write(frame.encode())
                await asyncio.sleep(0.05)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            return response

    async def environment(self, request):
        """Read-only loopback adapter; environment acquisition survives GPU outages."""
        import aiohttp
        zone = request.query.get('zone_id', 'lab')
        if not __import__('re').fullmatch(r'[a-zA-Z0-9_-]{1,40}', zone):
            return web.json_response({'ok': False, 'error': 'invalid zone_id'}, status=400)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as client:
                async with client.get('http://127.0.0.1:28984/env/v1/latest', params={'zone_id': zone}) as response:
                    body = await response.json()
                    return web.json_response(body, status=response.status, headers={'Cache-Control': 'no-store'})
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return web.json_response({'ok': False, 'error': 'environment gateway unavailable'}, status=503)

    async def merge_fragments(self, request):
        """Human merge of two adjacent transcript fragments (originals are kept)."""
        body = await request.json()
        ids = body.get('event_ids') or []
        if not isinstance(ids, list) or len(ids) != 2:
            raise web.HTTPBadRequest(text='event_ids must list exactly two transcripts')
        with self.lock:
            try:
                merged = merge.merge_transcripts(list(self.events), ids[0], ids[1])
            except merge.MergeError as ex:
                raise web.HTTPConflict(text=str(ex))
            existing = merge.existing_merge(self.events, merged['merged_event_id'])
            if existing is not None:
                return web.json_response(existing)      # idempotent: same pair, same event
            journal('events.jsonl', merged)
            self.events.appendleft(merged)
        publish(self.client, 'results', merged, expiry=60)
        return web.json_response(merged)

    async def confirm(self, request):
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid json')
        with self.lock:
            try:
                x, created = confirm_transcript(list(self.events), self.confirmed, body)
            except ConfirmationError as ex:
                raise web.HTTPBadRequest(text=str(ex))
            if created:
                self.events.appendleft(x)
                journal('events.jsonl', x)
                publish(self.client, 'feedback', x)
        return web.json_response(dict(x, already_confirmed=not created))


def main():
    warm_capture()   # one probe, off the capture path
    e = Edge()
    app = web.Application(client_max_size=16384)
    app.add_routes([web.get('/mm/api/state',e.state), web.get('/mm/api/stream',e.stream),
                 web.get('/mm/api/environment',e.environment), web.get('/mm/api/feedback_state',e.feedback_state), web.post('/mm/api/start',e.start),
                    web.post('/mm/api/stop',e.stop_capture), web.post('/mm/api/confirm',e.confirm),
                 web.post('/mm/api/merge',e.merge_fragments),
                    web.get('/mm',lambda r:web.FileResponse(ROOT/'page.html'))])
    # Keep an access trail: without it, an unexpected recording cannot be attributed
    # to a caller, which is exactly the gap found on 2026-11-03.
    results = ROOT/'results'
    results.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(str(results/'access.log'), maxBytes=5*1024*1024,
                                                   backupCount=2, encoding='utf8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    access = logging.getLogger('aiohttp.access')
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    web.run_app(app, host='127.0.0.1', port=28991, access_log=access)

if __name__ == '__main__':
    main()
