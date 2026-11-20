"""R2 回归：拆分后视觉观测/事件必须真的进入数据面归档。

复现复核意见 R2（修复前必红）：
独立模式下 `LocalPreview.archive` 恒为 None，worker 里所有
`if local_preview.archive: archive.submit(...)` 分支被跳过；DatasetApi 也没有记录接收路由。
结果是"写线程存在但没有数据来源"，只能导出空包。

本测试用真实的 DatasetApi + 真实的 LocalPreview（loopback HTTP），从 preview.archive.submit
投递观测，断言记录确实落进 experiment_records；并覆盖去重、断连丢弃计数。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.dataset_api import DatasetApi  # noqa: E402
from lab.local_preview import LocalPreview  # noqa: E402

TEST_CONFIG = {"project": "abc20261102", "topic_root": "labs/abc20261102/v1",
               "mqtt_host": "127.0.0.1", "mqtt_port": 18830, "llm_model": "qwen2.5:7b",
               "sample_fps": 5, "capture_fps": 15, "max_people": 0, "pose_max_det": 4,
               "min_person_confidence": 0.2, "min_person_height_ratio": 0.1,
               "replay_file": "data/replay.mp4", "width": 1280, "height": 720}


def sandbox():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data" / "uploads").mkdir(parents=True)
    source = tmp / "env.sqlite3"
    with sqlite3.connect(str(source)) as db:
        db.execute("CREATE TABLE measurements(received_at_ms INTEGER)")
        db.commit()
    (tmp / "config.json").write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
    os.environ["ABC_CONFIG"] = str(tmp / "config.json")
    return tmp, source


def observation(seq, run="r2"):
    return {"run_id": run, "collection_batch_id": run, "producer_instance_id": "edge-1",
            "observation_seq": seq, "source_frame_id": seq,
            "tracks": [{"bbox": [10.0, 20.0, 30.0, 40.0], "confidence": 0.6, "class_name": "cup"}]}


def wait_for(predicate, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def count(store, batch):
    with sqlite3.connect(str(store.path)) as db:
        return db.execute("SELECT COUNT(*) FROM experiment_records WHERE batch_id=?", (batch,)).fetchone()[0]


def test_split_mode_archive_reaches_dataset_api():
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    preview = None
    try:
        preview = LocalPreview(port=0, dataset_api_url="http://127.0.0.1:%d" % api.port).start()
        assert preview.archive is not None, "拆分模式下 LocalPreview.archive 不能是 None（否则 worker 的归档调用被跳过）"
        preview.set_run("r2", {"run_id": "r2", "source": "replay", "generation": 1})
        for seq in range(1, 6):
            preview.archive.submit("observation", observation(seq))
        preview.archive.submit("event", {"run_id": "r2", "kind": "vision_ready", "producer": "edge"})
        assert wait_for(lambda: count(api.batch_store, "r2") >= 6), count(api.batch_store, "r2")
        with sqlite3.connect(str(api.batch_store.path)) as db:
            kinds = dict(db.execute("SELECT kind,COUNT(*) FROM experiment_records WHERE batch_id='r2' GROUP BY kind").fetchall())
        assert kinds.get("observation") == 5 and kinds.get("event") == 1, kinds
        print("PASS R2 归档链路连通：5 条观测 + 1 条事件经拆分后的入口进入数据面（%s）" % kinds)
    finally:
        if preview is not None:
            preview.close()
        api.stop()


def test_duplicate_delivery_is_deduplicated():
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    preview = None
    try:
        preview = LocalPreview(port=0, dataset_api_url="http://127.0.0.1:%d" % api.port).start()
        preview.set_run("r2", {"run_id": "r2", "source": "replay", "generation": 1})
        body = observation(7)
        preview.archive.submit("observation", body)
        preview.archive.submit("observation", dict(body))
        assert wait_for(lambda: count(api.batch_store, "r2") >= 1), count(api.batch_store, "r2")
        time.sleep(0.4)
        assert count(api.batch_store, "r2") == 1, "重复投递必须去重（同一 producer+seq），实际 %d 条" % count(api.batch_store, "r2")
        print("PASS R2 重复投递去重：同一 producer/seq 投两次只落 1 条")
    finally:
        if preview is not None:
            preview.close()
        api.stop()


def free_port():
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_outage_keeps_records_then_delivers():
    """R5：数据面短暂不可用时不能整批丢，恢复后要补投。"""
    tmp, source = sandbox()
    port = free_port()
    api = DatasetApi(root=tmp, env_db=source, port=port).start()
    preview = None
    try:
        preview = LocalPreview(port=0, dataset_api_url="http://127.0.0.1:%d" % port).start()
        preview.set_run("r2", {"run_id": "r2", "source": "replay", "generation": 1})
        for seq in range(1, 4):
            preview.archive.submit("observation", observation(seq))
        assert wait_for(lambda: count(api.batch_store, "r2") >= 3, timeout=6), count(api.batch_store, "r2")
        api.stop()
        time.sleep(0.3)
        for seq in range(4, 9):                      # 故障窗口内的 5 条
            preview.archive.submit("observation", observation(seq))
        deadline = time.monotonic() + 8          # 投递线程可能正卡在一次 2 s 超时里
        stats = {}
        while time.monotonic() < deadline:
            stats = dict(preview.archive.stats)
            if stats["backlog"] >= 5 or stats["failed_attempts"] > 0:
                break
            time.sleep(0.1)
        assert stats["dropped"] == 0, "故障期间不允许丢记录：%s" % stats
        assert stats["backlog"] >= 5 or stats["failed_attempts"] > 0, "故障期间应有积压或失败尝试：%s" % stats
        api2 = DatasetApi(root=tmp, env_db=source, port=port).start()   # 数据面恢复
        try:
            assert wait_for(lambda: count(api2.batch_store, "r2") >= 8, timeout=20), count(api2.batch_store, "r2")
            assert preview.archive.stats["dropped"] == 0, preview.archive.stats
            print("PASS R5 断连不丢：故障期积压 %s，恢复后 8 条全部入库、dropped=0"
                  % stats["backlog"])  # noqa: E501
        finally:
            api2.stop()
    finally:
        if preview is not None:
            preview.close()
        try:
            api.stop()
        except Exception:  # noqa: BLE001 - 已停止
            pass


def test_flush_drains_before_session_end():
    """R5：会话结束前的 flush() 必须把队列排空，结算才能看到完整观测。"""
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    preview = None
    try:
        preview = LocalPreview(port=0, dataset_api_url="http://127.0.0.1:%d" % api.port).start()
        preview.set_run("rf", {"run_id": "rf", "source": "replay", "generation": 1})
        for seq in range(120):
            preview.archive.submit("observation", observation(seq, run="rf"))
        assert preview.archive.flush(timeout=10) is True, preview.archive.stats
        assert preview.archive.stats["backlog"] == 0, preview.archive.stats
        assert wait_for(lambda: count(api.batch_store, "rf") >= 120, timeout=10), count(api.batch_store, "rf")
        print("PASS R5 结束前冲刷：120 条在 flush() 返回时已全部入库")
    finally:
        if preview is not None:
            preview.close()
        api.stop()


if __name__ == "__main__":
    test_split_mode_archive_reaches_dataset_api()
    test_duplicate_delivery_is_deduplicated()
    test_outage_keeps_records_then_delivers()
    test_flush_drains_before_session_end()
