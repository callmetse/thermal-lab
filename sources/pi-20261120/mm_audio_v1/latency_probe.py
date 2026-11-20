"""Synthetic audio round trip: last chunk published -> matching sound_event back on the Pi.

What this measures (and what it does not):
  * metric name: `sound_trigger_chunk_to_pi_ms`
  * the tagger runs on a ~2 s cadence, so this is the latency of *sound events*, not of
    sentence transcription. Utterance-end -> transcript is a different metric and is
    not produced by this tool.
  * start point = the moment this host publishes the chunk (synthetic audio), so it
    excludes microphone capture and resampling.

Reporting rules learned the hard way:
  * packets that never come back are counted as losses, never dropped from the stats;
  * quartiles are computed in *receive order* (first/last quarter of the run), not on a
    sorted array, otherwise "slow down over time" is invisible by construction.
"""
import json
import math
import statistics
import struct
import sys
import time
import uuid

from common import TOPIC, mqtt, pack, publish

CHUNKS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
LABEL = sys.argv[2] if len(sys.argv) > 2 else 'run'
CHUNK_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25
RATE = 16000
FREQ = 440.0
ALLOW_BUSY = '--allow-busy' in sys.argv


def tone(seconds):
    frames = int(seconds * RATE)
    return b''.join(struct.pack('<h', int(32767 * 0.25 * math.sin(2 * math.pi * FREQ * i / RATE)))
                    for i in range(frames))


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))])


def segments_in_order(rtts):
    """First/second half and quarters by receive order: shows drift, hides nothing."""
    if not rtts:
        return None, None
    half = len(rtts) // 2
    halves = [round(statistics.median(rtts[:half])), round(statistics.median(rtts[half:]))] if half else None
    quarters = []
    for i in range(4):
        chunk = rtts[i * len(rtts) // 4:(i + 1) * len(rtts) // 4]
        if chunk:
            quarters.append(round(statistics.median(chunk)))
    return halves, quarters


def main():
    session = uuid.uuid4().hex
    sent = {}
    rows = []

    def on_message(client, userdata, msg):
        try:
            x = json.loads(msg.payload)
        except Exception:
            return
        if x.get('session_id') != session or x.get('kind') != 'sound_events':
            return
        seq = (x.get('source') or {}).get('seq')
        if seq not in sent or seq in {row['seq'] for row in rows}:
            return
        rows.append({
            'seq': seq,
            'recv_mono_ns': time.monotonic_ns(),
            'rtt_ms': round((time.monotonic_ns() - sent[seq]) / 1e6, 2),
            'worker_queue_ms': x.get('worker_queue_ms'),
            'infer_ms': x.get('infer_ms'),
        })

    client = mqtt('latency', on_message, [TOPIC + '/results', TOPIC + '/health'])
    deadline = time.time() + 10
    while not client.is_connected() and time.time() < deadline:
        time.sleep(0.2)
    if not client.is_connected():
        print(json.dumps({'label': LABEL, 'error': 'mqtt not connected'}))
        return 2
    time.sleep(2)

    chunk = tone(CHUNK_SECONDS)
    for seq in range(CHUNKS):
        due = time.monotonic()
        header = dict(session_id=session, seq=seq, sample_rate=RATE, format='S16_LE',
                      pi_end_mono_ns=due * 1e9, pi_start_mono_ns=(due - CHUNK_SECONDS) * 1e9,
                      time_basis='synthetic_latency_probe', parent_run_id=None,
                      pi_wall_ms=time.time() * 1000, hardware_sample_rate=RATE,
                      synthetic=True, probe_label=LABEL)
        sent[seq] = time.monotonic_ns()
        publish(client, 'audio', pack(header, chunk), expiry=30, qos=0)
        wait = time.monotonic() + CHUNK_SECONDS
        while time.monotonic() < wait:
            time.sleep(0.005)
    publish(client, 'commands', {'op': 'flush', 'session_id': session, 'synthetic': True}, expiry=30)
    time.sleep(6)
    client.loop_stop()

    # The tagger emits one sound_event per MM_TAGGING_INTERVAL_S (2 s by default), so the
    # expected number of results is chunks*CHUNK_SECONDS/tagging, not one per chunk.
    tagging = float(__import__('os').environ.get('MM_TAGGING_INTERVAL_S', '2.0'))
    expected = max(1, int((CHUNKS * CHUNK_SECONDS) // tagging))
    received = sum(1 for seq in sent if seq in {row['seq'] for row in rows})
    missing = max(0, expected - received)
    ordered_rtts = [row['rtt_ms'] for row in rows]          # receive order
    halves, quarters = segments_in_order(ordered_rtts)
    queue = [row['worker_queue_ms'] or 0 for row in rows]
    infer = [row['infer_ms'] or 0 for row in rows]
    out = {
        'label': LABEL,
        'metric': 'sound_trigger_chunk_to_pi_ms',
        'note': 'synthetic chunk publish -> sound_event back on this host; excludes capture and sentence ASR',
        'chunks': CHUNKS,
        'expected_results': expected,
        'received': received,
        'missing_results': missing,
        'loss_rate': round(missing / expected, 3),
        'rtt_ms': {'p50': pct(ordered_rtts, 0.5), 'p90': pct(ordered_rtts, 0.9),
                   'p95': pct(ordered_rtts, 0.95), 'max': max(ordered_rtts) if ordered_rtts else None,
                   'min': min(ordered_rtts) if ordered_rtts else None},
        'halves_p50_receive_order': halves,
        'quarters_p50_receive_order': quarters,
        'worker_queue_ms': {'p50': pct(queue, 0.5), 'max': max(queue) if queue else None},
        'infer_ms': {'p50': pct(infer, 0.5), 'max': max(infer) if infer else None},
        'session': session,
    }
    path = '/home/pi/mm_audio_v1/results/latency_%s.jsonl' % LABEL
    with open(path, 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    out['jsonl'] = path
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
