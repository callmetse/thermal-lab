"""Measure the current ABC vision stack from the Pi side (read-only).

Two independent numbers, because they mean different things:
  * preview frames/s delivered on the browser channel (what a viewer perceives);
  * observation messages/s on MQTT (what the downstream pipeline receives).
Plus per-observation infer_ms and the share of observations that carry tracks.
"""
import asyncio
import json
import sys
import time

import aiohttp

from common import mqtt

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
samples = {'preview': [], 'obs': []}


async def ws_preview():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect('http://127.0.0.1:28990/live-video') as ws:
            end = time.time() + SECONDS
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    samples['preview'].append((time.time(), len(msg.data)))


def on_message(client, userdata, msg):
    try:
        payload = msg.payload
        if payload[:2] == b'\x1f\x8b':
            import gzip
            payload = gzip.decompress(payload)
        x = json.loads(payload)
    except Exception:
        return
    samples['obs'].append({'t': time.time(), 'infer_ms': x.get('infer_ms'),
                           'people': len(x.get('tracks') or []),
                           'status': x.get('status')})


def main():
    client = mqtt('measure', on_message, ['labs/abc20261102/v1/runs/+/observations'])
    time.sleep(1.5)
    started = time.time()
    asyncio.run(ws_preview())
    client.loop_stop()
    during = [o for o in samples['obs'] if started <= o['t'] <= started + SECONDS]
    prev = [p for p in samples['preview'] if started <= p[0] <= started + SECONDS]
    span = SECONDS
    print(json.dumps({
        'window_s': span,
        'preview_frames': len(prev),
        'preview_fps': round(len(prev) / span, 2),
        'preview_bytes_median': sorted(p[1] for p in prev)[len(prev) // 2] if prev else None,
        'observations': len(during),
        'observation_hz': round(len(during) / span, 2),
        'infer_ms_p50': round(sorted(o['infer_ms'] for o in during if o['infer_ms'])[
            len(during) // 2], 2) if during else None,
        'people_share': round(sum(1 for o in during if o['people'] >= 1) / len(during), 3) if during else None,
        'status_counts': {s: sum(1 for o in during if o['status'] == s) for s in {o['status'] for o in during}},
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
