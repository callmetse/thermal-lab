"""P2-09 回归：统计口径统一（quality=ok 才参与均值）。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.aggregation import RULE, is_valid  # noqa: E402
from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402

T0 = 1_800_000_000_000


def make(rows):
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    store = CollectionBatches(tmp / "batches.sqlite3", src)
    with sqlite3.connect(str(store.path)) as db:
        db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                      source_cursor,last_sync_ms,run_json) VALUES(?,?,?,?,?,?,'completed',0,?,?)''',
                   ("b1", "b1", "工位", "口径测试", "lab", T0, T0, json.dumps({"arm": "A"})))
        for i, (quality, values) in enumerate(rows):
            db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) '
                       'VALUES(?,?,?,?,?,?)',
                       ("b1", "e%d" % i, "sht4x", T0 + i, T0 + i,
                        json.dumps({"sensor_id": "sht4x", "quality": quality, "values": values})))
        db.commit()
    archive = ExperimentArchive(store, tmp)
    return store, archive


def test_rule_is_versioned():
    assert RULE["version"] == "quality-ok-v1"
    assert is_valid("ok") and not is_valid("stale") and not is_valid(None)
    print("PASS 规则带版本号，未知 quality 不算有效样本")


def test_summary_excludes_non_ok():
    store, archive = make([("ok", {"temperature_c": 20.0}),
                           ("ok", {"temperature_c": 22.0}),
                           ("stale", {"temperature_c": 30.0})])
    out = archive.summary("b1")
    stats = out["environment_statistics"]["sht4x/temperature_c"]
    assert stats["count"] == 2, stats
    assert abs(stats["mean"] - 21.0) < 1e-9, stats
    assert stats["min"] == 20.0 and stats["max"] == 22.0, stats
    assert out["environment_rows"] == 3 and out["excluded_by_quality"] == 1, out
    assert out["aggregation_rule"]["version"] == "quality-ok-v1"
    print("PASS 归档统计：stale 样本被排除（mean=21 而不是 24）")


def test_summary_reports_rule_even_when_empty():
    store, archive = make([("stale", {"temperature_c": 30.0})])
    out = archive.summary("b1")
    assert out["environment_statistics"] == {}, out
    assert out["excluded_by_quality"] == 1
    assert out["aggregation_rule"]["mean"] == "sample"
    print("PASS 全被排除时：统计为空但规则仍写清")


if __name__ == "__main__":
    test_rule_is_versioned()
    test_summary_excludes_non_ok()
    test_summary_reports_rule_even_when_empty()
