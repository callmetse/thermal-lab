"""Pipeline smoke test that does NOT use the microphone.

Publishes a synthetic 16 kHz sine session on the module's own MQTT prefix, asks
the worker to flush it, and prints every result that comes back. Used to prove
transport + worker model inference + collector journaling before any real room
audio is captured. Every header carries synthetic=true so nobody can mistake
these events for a real recording.

Run ON the Pi with the module venv:
    /home/pi/mm_audio_v1/.venv/bin/python -u /home/pi/mm_audio_v1/smoke_pipeline.py
"""
import json
import math
import struct
import sys
import time
import uuid

from common import TOPIC, event, mqtt, pack, publish

SECONDS = 16
CHUNK_S = 2
RATE = 16000


def tone(seconds, frequency=440.0, amplitude=0.25):
    frames = int(seconds * RATE)
    out = bytearray()
    for i in range(frames):
        v = int(32767 * amplitude * math.sin(2 * math.pi * frequency * i / RATE))
        out += struct.pack('<h', v)
    return bytes(out)


def main():
    sid = uuid.uuid4().hex
    seen = []
    started = time.time()

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        if data.get('session_id') != sid:
            return
        seen.append(data)
        print('%6.1fs %-24s %s' % (time.time() - started, data.get('kind'),
                                   json.dumps({k: v for k, v in data.items()
                                               if k in ('text', 'window_seconds', 'infer_ms',
                                                        'speaker', 'worker_queue_ms')},
                                              ensure_ascii=False)[:160]), flush=True)

    client = mqtt('smoke', on_message, [TOPIC + '/results', TOPIC + '/health'])
    deadline = time.time() + 10
    while not client.is_connected() and time.time() < deadline:
        time.sleep(0.2)
    if not client.is_connected():
        print('FAIL: cannot connect to the MQTT broker on 127.0.0.1:28883', flush=True)
        return 2
    time.sleep(2)  # let the retained-free health heartbeat arrive once
    print('session', sid, flush=True)

    chunk = tone(CHUNK_S)
    accepted = 0
    for seq in range(SECONDS // CHUNK_S):
        end = time.monotonic_ns()
        header = dict(session_id=sid, seq=seq, sample_rate=RATE, format='S16_LE',
                      pi_end_mono_ns=end, pi_start_mono_ns=end - int(len(chunk) / 32000 * 1e9),
                      time_basis='synthetic_test_no_capture', parent_run_id=None,
                      pi_wall_ms=time.time() * 1000, hardware_sample_rate=16000,
                      synthetic=True)
        accepted += int(bool(publish(client, 'audio', pack(header, chunk), expiry=30)))
        time.sleep(0.15)
    print('published %d/%d chunks' % (accepted, SECONDS // CHUNK_S), flush=True)
    publish(client, 'commands', {'op': 'flush', 'session_id': sid, 'synthetic': True}, expiry=30)

    time.sleep(20)
    kinds = {}
    for x in seen:
        kinds[x.get('kind')] = kinds.get(x.get('kind'), 0) + 1
    print('result kinds:', kinds, flush=True)
    client.loop_stop()
    ok = kinds.get('sound_events', 0) >= 3 and kinds.get('worker_session_flushed', 0) >= 1
    print('PASS' if ok else 'FAIL: expected >=3 sound_events and one worker_session_flushed', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
