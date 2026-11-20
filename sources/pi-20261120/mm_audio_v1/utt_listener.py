"""Listener on the Pi: timestamp every result event the moment it arrives.

Used once, for the end-to-end sentence metric: the transcript's arrival wall clock is
stamped here, so "speech end -> transcript on Pi" needs no cross-host clock agreement.
"""
import json
import time

from common import TOPIC, mqtt

OUT = '/tmp/utt_listener.jsonl'


def on_message(client, userdata, msg):
    if not msg.topic.endswith('/results'):
        return
    try:
        x = json.loads(msg.payload)
    except Exception:
        return
    if x.get('kind') not in ('transcript', 'merged_transcript'):
        return
    row = dict(received_wall_ms=time.time() * 1000,
               received_mono_ns=time.monotonic_ns(),
               kind=x.get('kind'),
               session=x.get('session_id'),
               text=x.get('text'),
               endpoint_mode=x.get('endpoint_mode'),
               speech_start_sample=(x.get('source') or {}).get('seq'),
               worker_recorded_wall_ms=x.get('recorded_wall_ms'))
    with open(OUT, 'a') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    client = mqtt('utt-listener', on_message, [TOPIC + '/results'])
    deadline = time.time() + 120
    while not client.is_connected() and time.time() < deadline:
        time.sleep(0.1)
    print('listening', flush=True)
    while time.time() < deadline:
        time.sleep(0.5)
    client.loop_stop()


if __name__ == '__main__':
    main()
