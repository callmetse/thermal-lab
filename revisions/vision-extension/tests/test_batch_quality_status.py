"""P1-08 回归：completed 不再等于"数据完整"。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.collection_batches import CollectionBatches  # noqa: E402

T0 = 1_800_000_000_000


def make(tmpdir, env_rows=0, capture_error=None, observations=3, gate_positive=None):
    src = Path(tmpdir) / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    store = CollectionBatches(Path(tmpdir) / "batches.sqlite3", src)
    with sqlite3.connect(str(store.path)) as db:
        db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                      source_cursor,last_sync_ms,run_json,capture_error,channel)
                      VALUES(?,?,?,?,?,?,'active',0,?,?,?,?)''',
                   ("b1", "b1", "工位", "测试", "lab", T0, T0, json.dumps({"arm": "A"}), capture_error, "A"))
        for i in range(env_rows):
            db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) '
                       'VALUES(?,?,?,?,?,?)', ("b1", "e%d" % i, "sht4x", T0, T0, "{}"))
        db.execute("CREATE TABLE IF NOT EXISTS experiment_records(id INTEGER PRIMARY KEY,batch_id TEXT,"
                   "kind TEXT,key TEXT,received_ms INTEGER,json TEXT,UNIQUE(batch_id,kind,key))")
        for i in range(observations):
            db.execute("INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)",
                       ("b1", "observation", "o%d" % i, T0, "{}"))
        if gate_positive is not None:
            payload = {"kind": "transport_stats", "gate_positive": gate_positive, "sampled": gate_positive}
            db.execute("INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)",
                       ("b1", "event", "stats", T0, json.dumps({"payload": payload})))
        db.commit()
    return store


def row(store):
    with sqlite3.connect(str(store.path)) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute('SELECT * FROM batches WHERE batch_id=?', ("b1",)).fetchone())


def test_normal_complete():
    s = make(tempfile.mkdtemp(), env_rows=5)
    out = s.finish("b1")
    assert out == {"execution_status": "completed", "archive_status": "ok", "data_quality": "good"}, out
    print("PASS 正常：completed / ok / good")


def test_capture_error_is_failed_not_complete():
    s = make(tempfile.mkdtemp(), env_rows=3, capture_error="low_disk_space")
    out = s.finish("b1")
    r = row(s)
    assert r["status"] == "completed", "会话本身结束了"
    assert out["execution_status"] == "failed" and out["data_quality"] == "poor", out
    assert "low_disk_space" in (r["quality_detail"] or "")
    print("PASS 采集异常：execution=failed、quality=poor（status 仍是 completed）")


def test_no_environment_is_degraded():
    s = make(tempfile.mkdtemp(), env_rows=0)
    out = s.finish("b1")
    assert out["archive_status"] == "partial" and out["data_quality"] == "degraded", out
    print("PASS 无环境记录：archive=partial、quality=degraded")


def test_zero_observations_is_poor():
    """R6：会话跑了但没有任何观测，不能判 good。"""
    s = make(tempfile.mkdtemp(), env_rows=5, observations=0)
    out = s.finish("b1")
    r = row(s)
    assert out["data_quality"] == "poor" and out["archive_status"] == "partial", out
    assert "no_observations" in (r["quality_detail"] or ""), r["quality_detail"]
    print("PASS R6 零观测：quality=poor、archive=partial，detail 标注 no_observations")


def test_cloud_observations_missing_is_marked():
    """R5 待办：门控有阳性样本却 0 条观测 → 必须显式标 cloud_observations_missing。"""
    s = make(tempfile.mkdtemp(), env_rows=5, observations=0, gate_positive=81)
    out = s.finish("b1")
    r = row(s)
    detail = json.loads(r["quality_detail"])
    assert out["data_quality"] == "poor" and out["archive_status"] == "partial", out
    assert detail.get("cloud_observations_missing") is True, detail
    assert detail.get("gate_positive") == 81, detail
    print("PASS R5 标记：gate_positive=81 且 0 观测 → cloud_observations_missing=true")


def test_no_cloud_marker_without_gate_positive():
    """没有门控阳性样本（空场景）时不能误报云端缺失。"""
    s = make(tempfile.mkdtemp(), env_rows=5, observations=0, gate_positive=0)
    s.finish("b1")
    detail = json.loads(row(s)["quality_detail"])
    assert "cloud_observations_missing" not in detail, detail
    assert detail.get("no_observations") is True, detail
    print("PASS R5 标记：gate_positive=0 时不误报云端缺失（只标 no_observations）")


def test_no_cloud_marker_when_observations_exist():
    s = make(tempfile.mkdtemp(), env_rows=5, observations=4, gate_positive=90)
    s.finish("b1")
    detail = json.loads(row(s)["quality_detail"])
    assert "cloud_observations_missing" not in detail, detail
    print("PASS R5 标记：有观测时不标云端缺失")


def test_ingest_loss_is_degraded():
    """R6：投递丢失数要进质量判定，不能当完整归档。"""
    s = make(tempfile.mkdtemp(), env_rows=5, observations=4)
    out = s.finish("b1", None, None, 2)
    r = row(s)
    assert out["data_quality"] == "degraded" and out["archive_status"] == "partial", out
    assert '"ingest_dropped": 2' in (r["quality_detail"] or ""), r["quality_detail"]
    print("PASS R6 投递丢失：quality=degraded、archive=partial，detail 记录 ingest_dropped=2")


def test_archive_error_dominates():
    s = make(tempfile.mkdtemp(), env_rows=4)
    out = s.finish("b1", None, "sqlite disk I/O error")
    assert out["archive_status"] == "error" and out["data_quality"] == "poor", out
    print("PASS 归档失败：archive=error、quality=poor")


def test_interrupted_reason():
    s = make(tempfile.mkdtemp(), env_rows=2)
    out = s.finish("b1", "interrupted")
    r = row(s)
    assert out["execution_status"] == "interrupted", out
    assert r["ended_reason"] == "interrupted"
    print("PASS 中断：execution=interrupted，原因落库")


def test_only_active_can_finish():
    s = make(tempfile.mkdtemp(), env_rows=1)
    s.finish("b1")
    before = row(s)
    again = s.finish("b1")
    after = row(s)
    assert before["ended_ms"] == after["ended_ms"], "重复 finish 不应改写结束时间"
    assert again["execution_status"] == "completed"
    print("PASS 幂等：重复 finish 不改写已结束批次")


if __name__ == "__main__":
    test_normal_complete()
    test_capture_error_is_failed_not_complete()
    test_no_environment_is_degraded()
    test_zero_observations_is_poor()
    test_cloud_observations_missing_is_marked()
    test_no_cloud_marker_without_gate_positive()
    test_no_cloud_marker_when_observations_exist()
    test_ingest_loss_is_degraded()
    test_archive_error_dominates()
    test_interrupted_reason()
    test_only_active_can_finish()
