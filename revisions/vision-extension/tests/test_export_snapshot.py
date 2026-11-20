"""P2-12 回归：导出必须是单批快照（计数与内容一致、不混入其他批次、不等全局队列）。"""
import io
import json
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402

T0 = 1_800_000_000_000


def make():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    store = CollectionBatches(tmp / "batches.sqlite3", src)
    archive = ExperimentArchive(store, tmp)   # 先建归档表（schema 由它创建）
    with sqlite3.connect(str(store.path)) as db:
        for bid in ("b1", "b2"):
            db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                          source_cursor,last_sync_ms,run_json) VALUES(?,?,?,?,?,?,'completed',0,?,?)''',
                       (bid, bid, "工位", "导出测试", "lab", T0, T0, json.dumps({"arm": "A"})))
        for i in range(3):
            db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) '
                       'VALUES(?,?,?,?,?,?)', ("b1", "e%d" % i, "sht4x", T0 + i, T0 + i,
                                               json.dumps({"sensor_id": "sht4x", "quality": "ok", "values": {"temperature_c": 20 + i}})))
        for i in range(5):
            db.execute('INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',
                       ("b1", "observation", "o%d" % i, T0 + i, json.dumps({"payload": {"tracks": []}})))
        for i in range(2):
            db.execute('INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',
                       ("b1", "event", "v%d" % i, T0 + i, json.dumps({"payload": {"kind": "x"}})))
        for i in range(2):
            db.execute('INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',
                       ("b2", "observation", "p%d" % i, T0 + i, json.dumps({"payload": {"tracks": []}})))
        db.commit()
    return store, archive


def read_zip(path):
    with zipfile.ZipFile(str(path)) as z:
        names = set(z.namelist())
        data = {name: z.read(name) for name in names}
    return names, data


def lines(blob):
    return [line for line in blob.decode("utf-8").splitlines() if line.strip()]


def test_export_snapshot_consistency():
    store, archive = make()
    target = archive.export("b1")
    names, data = read_zip(target)
    summary = json.loads(data["summary.json"])
    obs = lines(data["observations.jsonl"])
    events = lines(data["events.jsonl"])
    env = lines(data["environment.jsonl"])
    assert len(obs) == summary["observation_count"] == 5, (len(obs), summary["observation_count"])
    assert len(events) == 2 and len(env) == 3
    assert summary["snapshot_version"] == "atomic-v1"
    assert summary["snapshot_boundary"]["max_record_id"] > 0
    # 记录以原始 payload 落盘（没有外层 batch_id），批次隔离由导出 SQL 保证：
    # b2 各 2 条记录不进包，因此 obs=5 就是 b1 自己的 5 条。
    assert all("payload" in json.loads(line) for line in obs)
    print("PASS 单批快照：obs 5 / events 2 / env 3 与 summary 完全一致，无 b2 泄漏")


def test_late_records_go_to_next_export_only():
    store, archive = make()
    first = archive.export("b1")
    _, data1 = read_zip(first)
    with sqlite3.connect(str(store.path)) as db:
        for i in range(2):
            db.execute('INSERT INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',
                       ("b1", "observation", "late%d" % i, T0 + 100 + i, json.dumps({"payload": {"tracks": []}})))
        db.commit()
    second = archive.export("b1")
    _, data2 = read_zip(second)
    assert len(lines(data1["observations.jsonl"])) == 5
    assert len(lines(data2["observations.jsonl"])) == 7, "晚到记录应进入下一次导出"
    s2 = json.loads(data2["summary.json"])
    assert s2["observation_count"] == 7
    print("PASS 晚到记录：第一次 5 条、加写后第二次 7 条，各自内部一致")


def test_zip_checksums_cover_members():
    store, archive = make()
    names, data = read_zip(archive.export("b1"))
    checks = json.loads(data["checksums.json"])
    assert "summary.json" in checks and "observations.jsonl" in checks
    assert set(checks) <= names
    print("PASS 包内校验和覆盖导出成员")


if __name__ == "__main__":
    test_export_snapshot_consistency()
    test_late_records_go_to_next_export_only()
    test_zip_checksums_cover_members()
