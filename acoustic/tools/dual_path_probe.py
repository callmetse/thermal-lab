"""Two MQTT round trips over two independent SSH tunnels, measured simultaneously.

Runs on the GPU host: same machine, same broker, same payload, same 4 Hz schedule,
but one client goes through the shared tunnel (28883, also carrying ABC vision) and
the other through a second, independent SSH connection (28884). Comparing them at the
same instant is the only way to tell whether sharing one SSH byte stream is what hurts.

Every packet is written to JSONL (route, seq, send/recv monotonic, rtt, timeout) so the
numbers can be recomputed instead of trusted.
"""
import argparse
import json
import threading
import time
import uuid

import paho.mqtt.client as mqtt

PAYLOAD = b'x' * 8000


class Route(threading.Thread):
    def __init__(self, tag, port, seconds, rate=4.0):
        super().__init__(daemon=True)
        self.tag = tag
        self.port = port
        self.seconds = seconds
        self.period = 1.0 / rate
        self.topic = 'labs/mm_audio_v1/v1/dual/' + uuid.uuid4().hex[:8]
        self.rows = []
        self.sent = {}
        self.subscribed = threading.Event()
        self.seq = 0

    def on_connect(self, client, userdata, flags, rc, properties):
        client.subscribe(self.topic, qos=0)

    def on_subscribe(self, client, userdata, mid, granted, properties):
        self.subscribed.set()

    def on_message(self, client, userdata, msg):
        try:
            seq = int.from_bytes(msg.payload[:8], 'big')
        except Exception:
            return
        sent_at = self.sent.get(seq)
        if sent_at is None:
            return
        now = time.monotonic()
        self.rows.append(dict(route=self.tag, seq=seq, sent=sent_at, recv=now,
                              rtt_ms=round((now - sent_at) * 1000, 2), timeout=False))

    def run(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id='dual-' + self.tag + '-' + uuid.uuid4().hex[:6],
                             protocol=mqtt.MQTTv5)
        client.on_connect = self.on_connect
        client.on_subscribe = self.on_subscribe
        client.on_message = self.on_message
        client.connect('127.0.0.1', self.port, keepalive=20)
        client.loop_start()
        if not self.subscribed.wait(5.0):
            print('%s: no SUBACK on port %d' % (self.tag, self.port), flush=True)
            client.loop_stop()
            return
        start = time.monotonic()
        for i in range(int(self.seconds * 4)):
            target = start + i * self.period
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.seq += 1
            marker = self.seq
            self.sent[marker] = time.monotonic()
            client.publish(self.topic, marker.to_bytes(8, 'big') + PAYLOAD, qos=0)
        time.sleep(2.0)                     # grace for the tail, then count the rest as lost
        for marker, sent_at in self.sent.items():
            if not any(row['seq'] == marker for row in self.rows):
                self.rows.append(dict(route=self.tag, seq=marker, sent=sent_at, recv=None,
                                      rtt_ms=None, timeout=True))
        client.loop_stop()
        client.disconnect()


def summarise(rows):
    rtts = sorted(r['rtt_ms'] for r in rows if r['rtt_ms'] is not None)
    timeouts = sum(1 for r in rows if r['timeout'])
    pick = lambda q: rtts[min(len(rtts) - 1, int(q * len(rtts)))] if rtts else None
    return dict(n=len(rows), returned=len(rtts), timeouts=timeouts,
                timeout_rate=round(timeouts / len(rows), 3) if rows else None,
                p50=pick(0.5), p90=pick(0.9), p95=pick(0.95),
                max=rtts[-1] if rtts else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--label', required=True)
    parser.add_argument('--port-a', type=int, default=28883)
    parser.add_argument('--port-b', type=int, default=28884)
    args = parser.parse_args()

    a = Route('shared', args.port_a, args.seconds)
    b = Route('independent', args.port_b, args.seconds)
    a.start()
    b.start()
    a.join()
    b.join()

    rows = a.rows + b.rows
    path = '/root/mm_audio_v1/results/dualpath_%s.jsonl' % args.label
    with open(path, 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    print(json.dumps(dict(label=args.label, seconds=args.seconds,
                          shared=summarise(a.rows), independent=summarise(b.rows),
                          jsonl=path), ensure_ascii=False))


if __name__ == '__main__':
    main()
