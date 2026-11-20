#!/usr/bin/env python3
"""唤醒词监听：常听「小PI小PI」，命中后自动开一次采集。

三个都已实测踩过的坑，写在这里免得以后重踩：

1. 设备原生 48 kHz，用 `-r 16000` 会被**静默忽略**（2 秒回来 176400 字节 = 44.1 kHz），
   直接喂 16 kHz 的 KWS 模型会音高全错、永不命中。故 48 kHz 采 → 整数 3:1 抽取。
2. USB 声卡**独占**：本进程占着麦克风时 edge.py 开采集会报
   "capture or resampling failed"。故用 wake_pause / wake_ack 两个标志文件握手：
   edge 开麦前写 pause，本进程释放设备并写 ack，edge 才去开 arecord。
3. 唤醒命中后必须**先释放麦克风再请求开始采集**，否则同样抢不到设备。

用法：
    .venv/bin/python wake_word.py            # 前台跑
    .venv/bin/python wake_word.py --json     # 每行一个 JSON
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import sherpa_onnx

from audio_device import resolve as resolve_capture

M = '/home/pi/mm_audio_v1/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01'
KEYWORDS = '/home/pi/mm_audio_v1/keywords.txt'
ENC = f'{M}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'
DEC = f'{M}/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'
JOI = f'{M}/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'

SRC_RATE = 48000
DECIM = SRC_RATE // 16000
CHUNK_S = 0.25
STATE_URL = 'http://127.0.0.1:28991/mm/api/state'
START_URL = 'http://127.0.0.1:28991/mm/api/start'
START_SECONDS = 30
TRIGGER_COOLDOWN_S = 8.0
BUSY_POLL_S = 0.5

PAUSE_FLAG = Path('/home/pi/mm_audio_v1/data/wake_pause')
ACK_FLAG = Path('/home/pi/mm_audio_v1/data/wake_ack')


def build_spotter():
    return sherpa_onnx.KeywordSpotter(
        tokens=f'{M}/tokens.txt',
        encoder=ENC, decoder=DEC, joiner=JOI,
        keywords_file=KEYWORDS,
        num_threads=2,
        keywords_score=1.5,
        keywords_threshold=0.25,
        max_active_paths=4,
        provider='cpu',
    )


def http_json(url, data=None, timeout=2.0):
    req = urllib.request.Request(
        url,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'},
        method='GET' if data is None else 'POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def capture_running():
    try:
        st = http_json(STATE_URL)
        return st.get('status') in ('recording', 'stopping') or bool(st.get('active'))
    except Exception:
        return False


def trigger_capture(log):
    try:
        r = http_json(START_URL, {'seconds': START_SECONDS, 'speaker_mode': 'single'}, timeout=3)
        log('已开始采集 %s' % str(r)[:120])
    except Exception as exc:
        log('开始采集失败：%r' % (exc,))


def main():
    as_json = '--json' in sys.argv
    dev = resolve_capture()
    spotter = build_spotter()

    args = ['arecord', '-q', '-D', dev.alsa, '-t', 'raw',
            '-f', 'S16_LE', '-c', '1', '-r', str(SRC_RATE),
            '--period-size', '512', '--buffer-size', '8192']
    n_src_per_chunk = int(SRC_RATE * CHUNK_S)
    bytes_per_chunk = n_src_per_chunk * 2

    started = time.monotonic()
    last_trigger = -1e9
    proc = None
    stream = None

    def out(msg, t=None):
        tt = (t or time.monotonic()) - started
        if as_json:
            print(json.dumps({'event': msg, 't': round(tt, 2)}, ensure_ascii=False), flush=True)
        else:
            print(f'[{tt:7.2f}s] {msg}', flush=True)

    def open_mic():
        nonlocal stream
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stream = spotter.create_stream()
        return p

    def close_mic(p):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

    out('监听开始：' + dev.describe())
    proc = open_mic()

    try:
        while True:
            # edge.py 请求让路
            if PAUSE_FLAG.exists():
                close_mic(proc)
                proc = None
                try:
                    ACK_FLAG.write_text('released')
                except Exception:
                    pass
                while PAUSE_FLAG.exists():
                    time.sleep(0.05)
                try:
                    ACK_FLAG.unlink(missing_ok=True)
                except Exception:
                    pass
                out('让出麦克风，采集结束后继续监听')
                proc = open_mic()
                continue

            if proc is None:
                if capture_running():
                    time.sleep(BUSY_POLL_S)
                    continue
                proc = open_mic()
                continue

            # 兜底：没握手也发现有人在采集，就退让
            if capture_running():
                close_mic(proc)
                proc = None
                out('检测到采集进行中，暂时停止监听')
                while capture_running():
                    time.sleep(BUSY_POLL_S)
                proc = open_mic()
                out('采集结束，恢复监听')
                continue

            raw = proc.stdout.read(bytes_per_chunk)
            if not raw or len(raw) < bytes_per_chunk:
                out('麦克风读取中断，重开')
                close_mic(proc)
                time.sleep(1.0)
                proc = open_mic()
                continue

            src = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768
            usable = len(src) - len(src) % DECIM
            samples = src[:usable].reshape(-1, DECIM).mean(axis=1)

            stream.accept_waveform(16000, samples)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                res = spotter.get_result(stream)
                if not res:
                    continue
                now = time.monotonic()
                out(f'命中：{res}', now)
                spotter.reset_stream(stream)
                if now - last_trigger < TRIGGER_COOLDOWN_S:
                    out('冷却中，忽略重复唤醒', now)
                    continue
                last_trigger = now
                # 先让出设备，再请求开采集，否则 edge 抢不到麦克风
                close_mic(proc)
                proc = None
                time.sleep(0.05)
                trigger_capture(lambda m, _n=now: out(m, _n))
    except KeyboardInterrupt:
        pass
    finally:
        close_mic(proc)


if __name__ == '__main__':
    main()
