"""P2-11 基准：上传写盘是否阻塞 aiohttp 的 event loop（旧 vs 新）。

做法：同一个进程里起一个只挂上传路由的 aiohttp 服务，跑一个 10 ms 心跳协程量
"实际间隔 − 期望间隔"＝event loop 卡顿；同时用本地 HTTP 客户端推 N MB 数据。
旧实现直接在 loop 里 `out.write()`（SD 卡上每次 256 KB 会卡十几毫秒），新实现把
写盘丢进工作线程，卡顿应该显著变小。

用法（在 Pi 上跑，两份实现分别跑一次）：
  python tools/bench_upload_loop.py --variant old \
      --old-file state/p1-07-before/video_uploads.py --size-mb 256
  python tools/bench_upload_loop.py --variant new \
      --new-file lab/video_uploads.py --size-mb 256

输出：一行 JSON（p50/p95/max 卡顿、耗时、吞吐），便于脚本比对与留痕。
"""
import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, web


def load_old(path):
    spec = importlib.util.spec_from_file_location("video_uploads_old", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_new(path):
    root = Path(path).resolve().parent.parent
    sys.path.insert(0, str(root))
    from lab import video_uploads  # noqa: PLC0415 - 延迟导入，避免影响 old 模式
    return video_uploads


class SyncFile:
    """每写一块就 fsync：模拟"限速磁盘/写回压力"，两个变体同样处理。"""

    def __init__(self, handle):
        self._handle = handle

    def write(self, data):
        written = self._handle.write(data)
        os.fsync(self._handle.fileno())
        return written

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


def fake_inspect(path):
    """只看写盘路径：解码耗时与 loop 卡顿无关，两个变体都打桩。"""
    return {"duration_s": 1.0, "fps": 25.0, "width": 64, "height": 64}


async def measure(module, size_mb, chunk_kb, workdir=None, sync_each_chunk=False):
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="bench-upload-"))
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "data" / "uploads").mkdir(parents=True)
    module.ROOT = tmp
    module.inspect_video = fake_inspect
    module.MAX_BYTES = (size_mb + 64) * 1024 * 1024
    os.environ["ABC_UPLOAD_QUOTA_BYTES"] = str((size_mb + 64) * 1024 * 1024)
    if sync_each_chunk:
        # 两个变体同样处理：每块写后 fsync，把"页缓存吸收"这层挡掉，逼近限速磁盘。
        real_open = Path.open

        def patched_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            return SyncFile(handle) if "w" in str(mode) else handle

        Path.open = patched_open

    app = web.Application(client_max_size=1024 * 1024 * 1024)
    module.install(object(), app)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    lags = []
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            started = time.monotonic()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.01)
            except TimeoutError:
                pass
            lags.append(max(0.0, (time.monotonic() - started - 0.01) * 1000))

    chunk = b"z" * (chunk_kb * 1024)
    total_chunks = (size_mb * 1024) // chunk_kb
    tick = asyncio.create_task(ticker())
    started = time.monotonic()

    async def body():
        for _ in range(total_chunks):
            yield chunk

    async with ClientSession() as client:
        response = await client.post("http://127.0.0.1:%d/api/video-uploads" % port, data=body())
        status = response.status
        text = await response.text()
    elapsed = time.monotonic() - started
    stop.set()
    await asyncio.gather(tick, return_exceptions=True)
    await runner.cleanup()

    q = statistics.quantiles(lags, n=100) if len(lags) > 100 else None
    result = {
        "variant": None,
        "status": status,
        "size_mb": size_mb,
        "elapsed_s": round(elapsed, 3),
        "throughput_mb_s": round(size_mb / elapsed, 1) if elapsed else None,
        "samples": len(lags),
        "lag_p50_ms": round(statistics.median(lags), 2) if lags else None,
        "lag_p95_ms": round(q[94], 2) if q else None,
        "lag_max_ms": round(max(lags), 2) if lags else None,
        "error": None if status == 201 else text[:200],
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["old", "new"], required=True)
    parser.add_argument("--old-file", default="state/p1-07-before/video_uploads.py")
    parser.add_argument("--new-file", default="lab/video_uploads.py")
    parser.add_argument("--size-mb", type=int, default=256)
    parser.add_argument("--chunk-kb", type=int, default=256)
    parser.add_argument("--sync-each-chunk", action="store_true",
                        help="每块写完 fsync（模拟限速磁盘）")
    parser.add_argument("--dir", default=None, help="上传落盘目录（默认系统临时目录；Pi 上应指到 SD 卡）")
    args = parser.parse_args()
    module = load_old(args.old_file) if args.variant == "old" else load_new(args.new_file)
    result = asyncio.run(measure(module, args.size_mb, args.chunk_kb, args.dir, args.sync_each_chunk))
    result["dir"] = args.dir or tempfile.gettempdir()
    result["variant"] = args.variant
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
