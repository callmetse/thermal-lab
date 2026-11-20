"""Compare two transport paths for the audio stream while vision traffic competes.

Run on the Pi. Two probes, same 8 KB payload, 4 Hz:
  * control path: MQTT publish/subscribe loopback through the control-cloud broker
                  (exactly the hops the audio uses: Pi -> control -> Pi)
  * direct path : TCP echo to the GPU host over a dedicated SSH tunnel
                  (Pi -> GPU, bypassing the control cloud)

The probe reports p50/p90/max per path so the two options can be compared under the
same vision load instead of argued about.
"""
import argparse
import json
import socket
import statistics
import time
import uuid

import paho.mqtt.client as mqtt


def probe_control(duration, payload):
    topic = 'labs/mm_audio_v1/v1/probe/' + uuid.uuid4().hex[:8]
    pending = {}

    def on_message(client, userdata, msg):
        marker = int.from_bytes(msg.payload[:8], 'big')
        if marker in pending:
            pending[marker].append(time.monotonic())

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='probe-control-' + uuid.uuid4().hex[:6], protocol=mqtt.MQTTv5)
    client.on_message = on_message
    client.connect('127.0.0.1', 28883, keepalive=20)
    client.subscribe(topic, qos=0)
    client.loop_start()
    time.sleep(1.0)

    sent = []
    started = time.monotonic()
    index = 0
    while time.monotonic() - started < duration:
        marker = int(time.monotonic_ns() % 10 ** 9)
        pending[marker] = [time.monotonic()]
        client.publish(topic, marker.to_bytes(8, 'big') + payload, qos=0)
        sent.append(marker)
        index += 1
        time.sleep(0.25)
    time.sleep(1.5)
    client.loop_stop()
    client.disconnect()
    rtts = [round((entry[1] - entry[0]) * 1000, 1)
            for marker, entry in pending.items() if len(entry) > 1]
    return rtts


def probe_direct(duration, payload, host='127.0.0.1', port=19001):
    rtts = []
    sock = socket.create_connection((host, port), timeout=5)
    started = time.monotonic()
    try:
        while time.monotonic() - started < duration:
            stamp = time.monotonic()
            sock.sendall(int(stamp * 1e6).to_bytes(8, 'big') + payload)
            reply = sock.recv(len(payload) + 8)
            if len(reply) == len(payload) + 8:
                rtts.append(round((time.monotonic() - stamp) * 1000, 1))
            time.sleep(0.25)
    finally:
        sock.close()
    return rtts


def summarise(name, rtts):
    if not rtts:
        return dict(path=name, n=0)
    ordered = sorted(rtts)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return dict(path=name, n=len(ordered), p50=pick(0.5), p90=pick(0.9),
                max=ordered[-1], mean=round(statistics.mean(ordered), 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=40)
    parser.add_argument('--label', default='vision-off')
    parser.add_argument('--tag', default='')
    parser.add_argument('--skip-direct', action='store_true')
    parser.add_argument('--skip-control', action='store_true')
    args = parser.parse_args()

    payload = b'x' * 8000
    result = dict(label=args.label, tag=args.tag, at=time.strftime('%H:%M:%S'))
    if not args.skip_control:
        result['control'] = summarise('control-cloud', probe_control(args.seconds, payload))
    if not args.skip_direct:
        result['direct'] = summarise('pi-to-gpu', probe_direct(args.seconds, payload))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
