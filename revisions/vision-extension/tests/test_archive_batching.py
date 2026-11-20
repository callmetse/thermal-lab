"""P2-11 回归：归档与上传不阻塞实时链路。

复现审查原文 P2-11（修复前本文件必然失败）：
1. `archive.fail` 在调用线程同步写 SQLite —— 队列满时由 MQTT 回调线程直接下单条写；
2. 每条记录单独连接、单独提交 —— 慢 SD 卡上写锁竞争会拖慢实时链路；
3. `LocalPreview._call` 超时 5 s 且超时后的 coroutine 未显式取消 —— 迟到的启动会改状态、留下幽灵批次；
4. `archive.begin` 为了记素材哈希把整段视频再读一遍；
5. 上传的文件写操作直接在 aiohttp loop 里同步执行。

用法：python tests/test_archive_batching.py
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from lab import experiment_archive, video_uploads  # noqa: E402
from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402
from lab.local_preview import LocalPreview  # noqa: E402

T0 = 1_800_000_000_000


def make():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    store = CollectionBatches(tmp / "batches.sqlite3", src)
    archive = ExperimentArchive(store, tmp)
    with sqlite3.connect(str(store.path)) as db:
        db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                      source_cursor,last_sync_ms,run_json) VALUES(?,?,?,?,?,?,'completed',0,?,?)''',
                   ("b1", "b1", "工位", "批量测试", "lab", T0, T0, json.dumps({"arm": "A"})))
        db.commit()
    return tmp, store, archive


class CountingStore:
    """代理 store.connect，统计"单条 execute"与"批量 executemany"的真实次数。"""

    def __init__(self, store):
        self._store = store
        self.counts = {"executes": 0, "executemany": 0, "rows": 0, "commits": 0, "connects": 0}

    @property
    def path(self):
        return self._store.path

    def connect(self):
        outer = self
        self.counts["connects"] += 1
        real = self._store.connect()

        class Proxy:
            def execute(self, *args, **kwargs):
                outer.counts["executes"] += 1
                return real.execute(*args, **kwargs)

            def executemany(self, *args, **kwargs):
                outer.counts["executemany"] += 1
                outer.counts["rows"] += len(args[1])
                return real.executemany(*args, **kwargs)

            def commit(self):
                outer.counts["commits"] += 1
                return real.commit()

            def __getattr__(self, name):
                return getattr(real, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                real.close()
                return False

        return Proxy()


def record_rows(store, batch_id="b1"):
    with sqlite3.connect(str(store.path)) as db:
        return db.execute("SELECT COUNT(*) FROM experiment_records WHERE batch_id=?", (batch_id,)).fetchone()[0]


def error_rows(store, batch_id="b1"):
    with sqlite3.connect(str(store.path)) as db:
        row = db.execute("SELECT error FROM experiment_errors WHERE batch_id=?", (batch_id,)).fetchone()
    return row[0] if row else None


def write_test_config(tmp):
    """begin() 需要一份合法 config.json；本机没有部署机的配置，用最小合法配置替代。"""
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps({
        "project": "abc20261102", "topic_root": "labs/abc20261102/v1",
        "mqtt_host": "127.0.0.1", "mqtt_port": 18830, "llm_model": "qwen2.5:7b",
        "sample_fps": 5, "capture_fps": 15, "max_people": 0, "pose_max_det": 4,
        "min_person_confidence": 0.2, "min_person_height_ratio": 0.1,
        "replay_file": "data/replay.mp4", "width": 1280, "height": 720,
    }), encoding="utf-8")
    return path


class LoopHarness:
    """只起 LocalPreview 的 event loop，不起 HTTP（端口留给真机）。"""

    def __init__(self):
        self.preview = LocalPreview()
        self.preview.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.preview.loop)
        self.preview.loop.run_forever()

    def stop(self):
        self.preview.loop.call_soon_threadsafe(self.preview.loop.stop)
        self.thread.join(5)


class FakeStore:
    """可注入延迟的批次库替身：用来制造"启动慢 / 超时"。"""

    def __init__(self, slow_runs=(), start_delay=0.0):
        self.slow_runs = set(slow_runs)
        self.start_delay = start_delay
        self.started = []
        self.finished = []

    def start(self, run):
        if run.get("run_id") in self.slow_runs:
            time.sleep(self.start_delay)
        self.started.append(run["run_id"])
        return "batch-" + run["run_id"]

    def finish(self, batch_id, reason=None, archive_error=None):
        self.finished.append((batch_id, archive_error))

    def sync(self, batch_id):
        return 0

    def get(self, batch_id):
        return {"capture_error": None}


class FakeArchive:
    def __init__(self, begin_delay=0.0):
        self.begin_delay = begin_delay
        self.begun = []

    def begin(self, run):
        time.sleep(self.begin_delay)
        self.begun.append(run["run_id"])

    def summary(self, batch_id):
        return {}

    def close(self):
        pass


# ---------------------------------------------------------------- 归档批量提交

def test_writer_uses_batched_commits():
    """120 条记录应合并成少量 executemany，而不是 120 次单条 insert+commit。"""
    tmp, store, archive = make()
    proxy = CountingStore(store)
    archive.store = proxy
    try:
        for i in range(120):
            archive.submit("observation", {"collection_batch_id": "b1", "producer_instance_id": "p",
                                           "observation_seq": i, "tracks": []})
        archive.pending.join()
        archive.close()          # 收尾时把不足一批的尾巴冲掉
        counts = dict(proxy.counts)
        assert record_rows(store) == 120, record_rows(store)
        assert counts["executes"] == 0, "写记录不应再走单条 execute：%s" % counts
        assert 2 <= counts["executemany"] <= 4, "120 条应按 50 条一批（2~4 次）提交：%s" % counts
        assert counts["rows"] == 120, counts
        assert archive.stats["records"] == 120 and archive.stats["commits"] == counts["executemany"], archive.stats
        print("PASS 归档批量提交：120 条 = %d 次 executemany、%d 行，无单条 execute"
              % (counts["executemany"], counts["rows"]))
    finally:
        archive.close()
        store_path = store.path


def test_fail_never_blocks_caller_thread():
    """队列满/写失败时 fail() 只能投递，不能在调用线程里同步写库。"""
    tmp, store, archive = make()
    real_connect = store.connect

    def slow_connect():
        time.sleep(0.4)
        return real_connect()

    archive.store = types.SimpleNamespace(connect=slow_connect, path=store.path)
    started = time.monotonic()
    archive.fail("b1", "archive_queue_or_payload_error: Full")
    elapsed = time.monotonic() - started
    assert elapsed < 0.1, "fail() 在调用线程阻塞了 %.3f s" % elapsed
    archive.pending.join()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and error_rows(store) is None:
        time.sleep(0.02)
    assert error_rows(store) == "archive_queue_or_payload_error: Full", error_rows(store)
    print("PASS fail 只投递：调用线程 %.3f s 返回，错误随后落库" % elapsed)


def test_low_disk_records_error_instead_of_lost_silence():
    """低盘时记录进 experiment_errors（既有行为，批量重写后必须保持）。"""
    tmp, store, archive = make()
    old = experiment_archive.shutil.disk_usage
    experiment_archive.shutil.disk_usage = lambda path: types.SimpleNamespace(free=1024)
    try:
        for i in range(3):
            archive.submit("observation", {"collection_batch_id": "b1", "producer_instance_id": "p",
                                           "observation_seq": i, "tracks": []})
        archive.pending.join()
        archive.close()      # 在低盘打桩生效时收尾，确保这批被判定为低盘
    finally:
        experiment_archive.shutil.disk_usage = old
    assert record_rows(store) == 0, record_rows(store)
    assert "low_disk_space" in (error_rows(store) or ""), error_rows(store)
    print("PASS 低盘：记录进 experiment_errors，不静默丢")


# ---------------------------------------------------------------- begin 不再读整段视频

def test_begin_reuses_uploaded_sha256():
    """快照里的素材哈希必须复用上传时算好的 sha256，而不是把整段视频再读一遍。"""
    tmp = Path(tempfile.mkdtemp())
    uploads = tmp / "data" / "uploads"
    uploads.mkdir(parents=True)
    uid = "a" * 32
    (uploads / (uid + ".video")).write_bytes(b"x" * 4096)
    (uploads / (uid + ".json")).write_text(json.dumps({"upload_id": uid, "bytes": 4096,
                                                       "sha256": "d" * 64}), encoding="utf-8")
    src = tmp / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.execute("CREATE TABLE measurements(received_at_ms INTEGER)")
        db.commit()
    store = CollectionBatches(tmp / "batches.sqlite3", src)
    archive = ExperimentArchive(store, tmp)
    hashed = []
    real_sha = experiment_archive.sha_file

    def spy(path):
        hashed.append(str(path))
        return real_sha(path)

    experiment_archive.sha_file = spy
    old_config = os.environ.get("ABC_CONFIG")
    os.environ["ABC_CONFIG"] = str(write_test_config(tmp))
    try:
        archive.begin({"run_id": "r1", "collection_batch_id": "b9", "source": "replay", "upload_id": uid})
    finally:
        experiment_archive.sha_file = real_sha
        if old_config is None:
            os.environ.pop("ABC_CONFIG", None)
        else:
            os.environ["ABC_CONFIG"] = old_config
    with sqlite3.connect(str(store.path)) as db:
        snapshot = json.loads(db.execute("SELECT json FROM experiment_snapshots WHERE batch_id='b9'").fetchone()[0])
    assets = snapshot["assets"]
    assert len(assets) == 1 and assets[0]["sha256"] == "d" * 64, assets
    assert assets[0]["bytes"] == 4096, assets
    assert not [p for p in hashed if p.endswith(".video")], "不应重新读取整段视频：%s" % hashed
    print("PASS begin 复用上传哈希：素材 sha256 来自元数据，没有重读视频")


# ---------------------------------------------------------------- 预览超时与迟到结果

def test_call_timeout_cancels_coroutine():
    """_call 超时必须显式取消协程，否则迟到的结果会继续改状态。"""
    h = LoopHarness()
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()

    try:
        started = time.monotonic()
        try:
            h.preview._call(slow(), timeout=0.15)
        except TimeoutError:
            pass
        else:
            raise AssertionError("超时未抛 TimeoutError")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, elapsed
        assert cancelled.wait(2.0), "超时后协程没有被取消（finally 未执行）"
        print("PASS _call 超时取消：%.2f s 抛错，协程在 2 s 内被取消" % elapsed)
    finally:
        h.stop()


def test_late_start_does_not_clobber_newer_run():
    """先发的启动比后发的慢：迟到者不能改状态，也不能把批次挂成幽灵。"""
    h = LoopHarness()
    store = FakeStore(slow_runs={"A"}, start_delay=0.5)
    h.preview.batch_store = store
    h.preview.archive = FakeArchive(begin_delay=0.0)
    first = threading.Thread(target=lambda: h.preview.set_run("A", {"run_id": "A"}), daemon=True)
    try:
        first.start()
        time.sleep(0.05)
        h.preview.set_run("B", {"run_id": "B"})
        first.join(5)
        time.sleep(0.3)
        assert h.preview.run_id == "B", h.preview.run_id
        assert h.preview.active_batch_id == "batch-B", h.preview.active_batch_id
        stale = [entry for entry in store.finished if entry[0] == "batch-A"]
        assert stale and stale[0][1] == "superseded_by_newer_run", store.finished
        print("PASS 迟到启动不改状态：run_id=B、active_batch_id=batch-B，batch-A 被显式收尾")
    finally:
        h.stop()


def test_timed_out_start_leaves_no_ghost_batch():
    """启动等待超时：已创建的批次必须被收尾，active_batch_id 不能留脏值。"""
    h = LoopHarness()
    store = FakeStore()
    h.preview.batch_store = store
    h.preview.archive = FakeArchive(begin_delay=5)
    try:
        try:
            h.preview.set_run("C", {"run_id": "C"}, timeout=0.2)
        except TimeoutError:
            pass
        else:
            raise AssertionError("启动超时未抛 TimeoutError")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not store.finished:
            time.sleep(0.02)
        assert ("batch-C", "start_cancelled") in store.finished, store.finished
        assert h.preview.active_batch_id is None, h.preview.active_batch_id
        print("PASS 启动超时不留幽灵批次：batch-C 以 start_cancelled 收尾")
    finally:
        h.stop()


# ---------------------------------------------------------------- 上传写盘不占 loop

def test_upload_write_happens_off_loop_thread():
    """上传的分块写盘必须在工作线程，不能占住 aiohttp 的 event loop。"""
    tmp = Path(tempfile.mkdtemp())
    uploads = tmp / "data" / "uploads"
    uploads.mkdir(parents=True)
    threads = []
    real_write = getattr(video_uploads, "_write_chunk", None)
    if real_write is None:
        raise AssertionError("缺少 _write_chunk：分块写盘没有独立入口")

    def spy(handle, chunk):
        threads.append(threading.current_thread().name)
        return real_write(handle, chunk)

    video_uploads._write_chunk = spy
    old_root, old_inspect = video_uploads.ROOT, video_uploads.inspect_video
    video_uploads.ROOT = tmp
    video_uploads.inspect_video = lambda path: dict(duration_s=1.0, fps=25.0, width=64, height=64)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    video_uploads.install(object(), app)
    outer = video_uploads

    try:
        async def scenario():
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/api/video-uploads", data=b"y" * 700_000)
                return response.status, await response.text()

        status, text = asyncio.run(scenario())
    finally:
        outer._write_chunk = real_write
        outer.ROOT, outer.inspect_video = old_root, old_inspect
    assert status == 201, (status, text)
    assert threads and all(name != threading.main_thread().name for name in threads), threads
    print("PASS 上传写盘在工作线程：%d 次分块写入，主线程 0 次" % len(threads))


if __name__ == "__main__":
    test_writer_uses_batched_commits()
    test_fail_never_blocks_caller_thread()
    test_low_disk_records_error_instead_of_lost_silence()
    test_begin_reuses_uploaded_sha256()
    test_call_timeout_cancels_coroutine()
    test_late_start_does_not_clobber_newer_run()
    test_timed_out_start_leaves_no_ghost_batch()
    test_upload_write_happens_off_loop_thread()
