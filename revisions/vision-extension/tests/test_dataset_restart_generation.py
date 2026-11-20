"""R3/R4 回归：重启接管与会话代次。

复现复核意见（修复前必红）：
- R3：`CollectionBatches.__init__` 无条件把 active 改成 interrupted，DatasetApi 随后再查
  active 自然查不到；"重启 vision-edge 后批次仍 active" 不能替代"重启数据服务后能接管"。
- R4：`set_session` 接收 generation 但只回显不校验；`run_id=None` 的结束请求也不核验目标会话，
  过期代次可以结束当前批次；重复 start 先 finish 当前批次再撞唯一键，不是幂等。
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.dataset_api import DatasetApi  # noqa: E402

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


def status_of(store, batch_id):
    with sqlite3.connect(str(store.path)) as db:
        row = db.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    return row[0] if row else None


def seed_active(tmp, source, run_id="live-1"):
    """用构造函数以外的方式造一个"上次进程留下的 active 批次"。"""
    path = tmp / "data" / "collection_batches.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    store = CollectionBatches(path, source, mark_interrupted=False)
    store.start({"run_id": run_id, "collection_name": "重启接管测试", "source": "replay"})
    with sqlite3.connect(str(store.path)) as db:
        db.execute("UPDATE batches SET status='active' WHERE run_id=?", (run_id,))
        db.commit()
    return store


def store_path(tmp):
    return tmp / "data" / "collection_batches.sqlite3"


# ---------------------------------------------------------------- R3 重启接管

def test_constructor_does_not_mutate_active_batches():
    """构造函数只建表/迁移，不得偷偷把活动批次改成中断。"""
    tmp, source = sandbox()
    seed_active(tmp, source)
    again = CollectionBatches(store_path(tmp), source, mark_interrupted=False)
    assert status_of(again, "live-1") == "active"
    print("PASS R3 构造函数不篡改状态：二次构造后批次仍 active")


def test_dataset_api_adopts_active_batch_on_restart():
    """数据服务重启：必须接管仍在采集的批次，而不是静默中断它。"""
    tmp, source = sandbox()
    seed_active(tmp, source)
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        assert status_of(api.batch_store, "live-1") == "active", status_of(api.batch_store, "live-1")
        assert api.active_batch_id == "live-1", api.active_batch_id
        state = api.state()
        assert state["adopted_on_restart"] is True, state
        print("PASS R3 重启接管：数据服务启动后接管 live-1（adopted_on_restart=true）")
    finally:
        api.stop()


def test_explicit_recovery_still_marks_interrupted():
    """显式恢复入口保留旧语义：愿意放弃旧批次时能标成 interrupted，并写原因。"""
    tmp, source = sandbox()
    store = seed_active(tmp, source)
    changed = store.recover_active_batches("preview_restart", keep_latest=False)
    assert changed == 1, changed
    assert status_of(store, "live-1") == "interrupted", status_of(store, "live-1")
    with sqlite3.connect(str(store.path)) as db:
        row = db.execute("SELECT execution_status,ended_reason FROM batches WHERE batch_id='live-1'").fetchone()
    assert row[0] == "interrupted" and row[1] == "preview_restart", row
    print("PASS R3 显式恢复保留旧语义：interrupted + ended_reason=preview_restart")


# ---------------------------------------------------------------- R4 代次与幂等

def test_stale_generation_stop_is_rejected():
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        api.submit_session("gen-new", {"run_id": "gen-new", "source": "replay"}, generation=10)
        assert status_of(api.batch_store, "gen-new") == "active"
        stale = False
        try:
            api.submit_session(None, generation=9)
        except Exception as error:  # noqa: BLE001 - 期望被拒
            stale = "过期" in str(error) or "stale" in str(error).lower() or "generation" in str(error).lower()
        assert stale, "旧代次 stop 必须被拒绝"
        assert status_of(api.batch_store, "gen-new") == "active", "旧 stop 不能结束当前批次"
        print("PASS R4 旧代次 stop 被拒：批次仍 active")
    finally:
        api.stop()


def test_stale_generation_start_is_rejected():
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        api.submit_session("new-run", {"run_id": "new-run", "source": "replay"}, generation=10)
        rejected = False
        try:
            api.submit_session("old-run", {"run_id": "old-run", "source": "replay"}, generation=3)
        except Exception:  # noqa: BLE001 - 期望被拒
            rejected = True
        assert rejected, "旧代次 start 必须被拒绝"
        assert api.active_batch_id == "new-run", api.active_batch_id
        assert status_of(api.batch_store, "old-run") is None, "被拒的 start 不能建批次"
        print("PASS R4 旧代次 start 被拒：当前会话不受影响，也没有建出批次")
    finally:
        api.stop()


def test_duplicate_start_is_idempotent():
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        first = api.submit_session("dup-run", {"run_id": "dup-run", "source": "replay"}, generation=1)
        second = api.submit_session("dup-run", {"run_id": "dup-run", "source": "replay"}, generation=1)
        assert first["active_batch_id"] == second["active_batch_id"] == "dup-run", (first, second)
        assert status_of(api.batch_store, "dup-run") == "active", "重复 start 不得结束当前批次"
        print("PASS R4 重复 start 幂等：同一 run_id 返回同一活动批次，不结算")
    finally:
        api.stop()


def test_completed_run_id_rejected_without_side_effects():
    """重复 start 一个已完成的 run_id：拒绝，且不能把当前活动批次结算掉。"""
    tmp, source = sandbox()
    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        api.submit_session("done-run", {"run_id": "done-run", "source": "replay"}, generation=1)
        api.submit_session(None, generation=1)
        api.submit_session("now-run", {"run_id": "now-run", "source": "replay"}, generation=2)
        rejected = False
        try:
            api.submit_session("done-run", {"run_id": "done-run", "source": "replay"}, generation=3)
        except Exception:  # noqa: BLE001 - 期望被拒
            rejected = True
        assert rejected, "已完成的 run_id 不能再次 start"
        assert api.active_batch_id == "now-run", api.active_batch_id
        assert status_of(api.batch_store, "now-run") == "active", "拒绝重复 start 时不得结算当前批次"
        print("PASS R4 已完成 run_id 的重复 start 被拒且无副作用")
    finally:
        api.stop()


if __name__ == "__main__":
    test_constructor_does_not_mutate_active_batches()
    test_dataset_api_adopts_active_batch_on_restart()
    test_explicit_recovery_still_marks_interrupted()
    test_stale_generation_stop_is_rejected()
    test_stale_generation_start_is_rejected()
    test_duplicate_start_is_idempotent()
    test_completed_run_id_rejected_without_side_effects()
