"""P1-07 回归：容量预算与导出限流。

复现审查原文 P1-07 的三条问题（修复前本文件必然失败）：
1. 上传只限单文件 512 MB，没有 `data/uploads` 总量配额，重复上传可以把盘吃满；
2. `experiment_archive.export` 直接 mkdtemp 写 JSONL+ZIP，无并发限制、无空间预估；
3. 进程崩溃留下的 `.part` 半成品没人清理，256 MiB 低盘保护也不覆盖导出。

用法：python tests/test_capacity_limits.py
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from lab import video_uploads  # noqa: E402
from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402
from lab import experiment_archive  # noqa: E402

T0 = 1_800_000_000_000
TOKEN = "fixture"


class Sandbox:
    """把 video_uploads.ROOT 指到临时目录，退出时还原。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.uploads = self.tmp / "data" / "uploads"
        self.uploads.mkdir(parents=True)
        self._old_root = video_uploads.ROOT

    def __enter__(self):
        video_uploads.ROOT = self.tmp
        return self

    def __exit__(self, *exc):
        video_uploads.ROOT = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)


def fake_inspect(path):
    """上传测试不解码视频：配额与写入路径才是本项的被测对象。"""
    return dict(duration_s=12.0, fps=25.0, width=1280, height=720)


def make_video_app():
    app = web.Application(client_max_size=1024 * 1024 * 1024)
    video_uploads.install(object(), app)
    return app


async def post_upload(app, body):
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/video-uploads", data=body)
        return response.status, await response.text()


def seed_existing_upload(folder, size):
    (folder / (TOKEN + ".video")).write_bytes(b"x" * size)
    (folder / (TOKEN + ".json")).write_text(json.dumps({"upload_id": TOKEN}), encoding="utf-8")


# ---------------------------------------------------------------- 上传总量配额

def test_upload_quota_rejects_beyond_budget():
    """已有 900 KB、配额 1 MB 时，再传 500 KB 必须 400 且不落盘。"""
    with Sandbox() as box:
        seed_existing_upload(box.uploads, 900_000)
        before = {p.name for p in box.uploads.iterdir()}
        os.environ["ABC_UPLOAD_QUOTA_BYTES"] = "1000000"
        old = video_uploads.inspect_video
        video_uploads.inspect_video = fake_inspect
        try:
            status, text = asyncio.run(post_upload(make_video_app(), b"y" * 500_000))
        finally:
            video_uploads.inspect_video = old
            os.environ.pop("ABC_UPLOAD_QUOTA_BYTES", None)
        assert status == 400, (status, text)
        message = json.loads(text)["error"]
        assert "配额" in message, message
        assert {p.name for p in box.uploads.iterdir()} == before, "超配额时不应留下任何文件"
        print("PASS 超配额上传被拒：400 且报「配额」，目录无残留")


def test_upload_within_budget_still_works():
    """配额内上传照常 201，且不留下 .part。"""
    with Sandbox() as box:
        seed_existing_upload(box.uploads, 1000)
        os.environ["ABC_UPLOAD_QUOTA_BYTES"] = "5000000"
        old = video_uploads.inspect_video
        video_uploads.inspect_video = fake_inspect
        try:
            status, text = asyncio.run(post_upload(make_video_app(), b"z" * 2_000_000))
        finally:
            video_uploads.inspect_video = old
            os.environ.pop("ABC_UPLOAD_QUOTA_BYTES", None)
        meta = json.loads(text)
        assert status == 201, (status, text)
        assert meta["bytes"] == 2_000_000 and len(meta["sha256"]) == 64
        names = [p.name for p in box.uploads.iterdir()]
        assert not [n for n in names if n.endswith(".part")], names
        expected = sum(p.stat().st_size for p in box.uploads.iterdir())
        assert video_uploads.uploads_bytes(box.uploads) == expected, (video_uploads.uploads_bytes(box.uploads), expected)
        print("PASS 配额内上传成功：201、sha256 正确、无 .part 残留")


def test_quota_message_is_actionable():
    """配额错误必须能直接指导操作：写明已用/配额/本次需要。"""
    message = video_uploads.quota_message(1900 * 1024 * 1024, 2 * 1024 ** 3, 300 * 1024 * 1024)
    assert "1.9 GiB" in message, message
    assert "2.0 GiB" in message, message
    assert "300.0 MiB" in message, message
    assert "capacity_report" in message, message
    assert video_uploads.quota_message(0, 2_000_000_000, 1000) is None
    print("PASS 配额提示可操作：写明已用/配额/本次与清理入口")


def test_partial_cleanup_on_install():
    """启动时清掉上次进程的 .part，正常文件不能动。"""
    with Sandbox() as box:
        (box.uploads / "dead1.part").write_bytes(b"x" * 10)
        (box.uploads / "dead2.part").write_bytes(b"x" * 20)
        (box.uploads / "keep.video").write_bytes(b"x" * 30)
        make_video_app()
        names = sorted(p.name for p in box.uploads.iterdir())
        assert names == ["keep.video"], names
        print("PASS 启动清理 .part 残留：只剩正常上传文件")


# ---------------------------------------------------------------- 导出并发与空间

def make_archive():
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
                   ("b1", "b1", "工位", "容量测试", "lab", T0, T0, json.dumps({"arm": "A"})))
        for i in range(40):
            db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) '
                       'VALUES(?,?,?,?,?,?)', ("b1", "e%d" % i, "sht4x", T0 + i, T0 + i,
                                               json.dumps({"sensor_id": "sht4x", "quality": "ok",
                                                           "values": {"temperature_c": 20 + i}})))
        for i in range(60):
            db.execute('INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',
                       ("b1", "observation", "o%d" % i, T0 + i, json.dumps({"payload": {"tracks": []}})))
        db.commit()
    return store, archive


def experiment_dirs():
    return {p.name for p in Path(tempfile.gettempdir()).glob("experiment-*")}


def test_export_serialized_by_semaphore():
    """两个线程同时导出：只有一个能跑，另一个明确报忙，且失败方不留临时目录。"""
    store, archive = make_archive()
    before = experiment_dirs()
    old = ExperimentArchive._settle_boundary

    def slow_settle(self, bid, timeout=2.0):
        time.sleep(0.4)
        return old(self, bid, timeout)

    results = []
    ExperimentArchive._settle_boundary = slow_settle

    def run():
        try:
            results.append(("ok", archive.export("b1")))
        except Exception as error:  # noqa: BLE001 - 记录被测异常
            results.append(("error", error))

    try:
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    finally:
        ExperimentArchive._settle_boundary = old

    oks = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]
    assert len(oks) == 1 and len(errors) == 1, results
    assert isinstance(errors[0][1], experiment_archive.ExportBusy), errors
    assert "导出" in str(errors[0][1]), errors
    created = experiment_dirs() - before
    assert len(created) == 1, "只有成功的那个导出应当建临时目录：%s" % created
    shutil.rmtree(oks[0][1].parent, ignore_errors=True)
    print("PASS 导出串行化：并发第二个 ExportBusy，临时目录只建 1 个")


def test_export_refuses_when_temp_space_low():
    """可用空间 < 估算 × 3 时必须先拒绝，不建临时目录、不写半个包。"""
    store, archive = make_archive()
    before = experiment_dirs()
    old = experiment_archive.shutil.disk_usage
    experiment_archive.shutil.disk_usage = lambda path: types.SimpleNamespace(free=1024 * 1024)
    try:
        archive.export("b1")
    except ValueError as error:
        text = str(error)
    else:
        raise AssertionError("空间不足时导出必须被拒绝")
    finally:
        experiment_archive.shutil.disk_usage = old
    assert "空间" in text, text
    assert experiment_dirs() - before == set(), "空间不足时不应残留临时目录"
    print("PASS 空间不足先拒绝：明确报错且无临时目录残留")


def test_export_estimate_covers_written_bytes():
    """估算必须≥实际落盘（否则空间检查形同虚设）。"""
    store, archive = make_archive()
    target = archive.export("b1")
    try:
        written = sum(p.stat().st_size for p in target.parent.iterdir() if p != target)
        boundary = (archive.summary("b1")["snapshot_boundary"])
        estimate = archive.estimate_export_bytes("b1", boundary["max_record_id"], boundary["max_env_id"])
        assert estimate >= written, (estimate, written)
        print("PASS 导出体积估算：%d B ≥ 实际 %d B" % (estimate, written))
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


# ---------------------------------------------------------------- 容量报告

def test_capacity_report_reports_four_categories():
    tmp = Path(tempfile.mkdtemp())
    try:
        uploads = tmp / "data" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "a.video").write_bytes(b"x" * 3000)
        (uploads / "b.video").write_bytes(b"x" * 2000)
        db = tmp / "data" / "collection_batches.sqlite3"
        with sqlite3.connect(str(db)) as conn:
            conn.executescript('''CREATE TABLE batches(batch_id TEXT PRIMARY KEY,run_id TEXT,location TEXT,name TEXT,
                zone_id TEXT,started_ms INTEGER,status TEXT,source_cursor INTEGER,last_sync_ms INTEGER,run_json TEXT);
                CREATE TABLE experiment_records(id INTEGER PRIMARY KEY,batch_id TEXT,kind TEXT,key TEXT,received_ms INTEGER,json TEXT);
                CREATE TABLE batch_environment(id INTEGER PRIMARY KEY,batch_id TEXT,event_id TEXT,sensor_id TEXT,
                data_time_ms INTEGER,received_at_ms INTEGER,raw_json TEXT);''')
            conn.commit()
        env_db = tmp / "env_sensor.sqlite3"
        env_db.write_bytes(b"x" * 5000)
        out = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "capacity_report.py"), "--root", str(tmp),
             "--env-db", str(env_db), "--json"],
            capture_output=True, text=True, encoding="utf-8")
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert set(data["categories"]) == {"original_environment", "structured_results", "media_uploads", "temp_exports"}
        assert data["categories"]["media_uploads"]["bytes"] == 5000, data["categories"]
        assert data["categories"]["original_environment"]["bytes"] == 5000, data["categories"]
        assert data["total_bytes"] >= 10000
        assert data["categories"]["media_uploads"]["quota_bytes"] > 0
        assert any("保留" in hint for hint in data["advice"]), data["advice"]
        print("PASS 容量报告：四类占用齐全、总字节自洽、含保留期建议")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_upload_quota_rejects_beyond_budget()
    test_upload_within_budget_still_works()
    test_quota_message_is_actionable()
    test_partial_cleanup_on_install()
    test_export_serialized_by_semaphore()
    test_export_refuses_when_temp_space_low()
    test_export_estimate_covers_written_bytes()
    test_capacity_report_reports_four_categories()
