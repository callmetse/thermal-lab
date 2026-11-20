"""P1-05（批次部分）回归：服务端筛选、分页与 has_more。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.collection_batches import CollectionBatches  # noqa: E402

T0 = 1_800_000_000_000


def make(tmpdir):
    src = Path(tmpdir) / "source.sqlite3"
    with sqlite3.connect(str(src)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    store = CollectionBatches(Path(tmpdir) / "batches.sqlite3", src)
    rows = [
        ("b1", "工位", "视觉与环境同步采集", T0, 0, None, "C"),
        ("b2", "实验室A", "上传视频验收", T0 + 3600_000, 1, None, "D"),
        ("b3", "实验室A", "夜间联调", T0 + 7200_000, 0, T0 + 7200_000 + 1000, "C"),
        ("b4", "管理功能验收专用", "收藏测试", T0 + 10800_000, 1, None, "A"),
    ]
    with sqlite3.connect(str(store.path)) as db:
        for bid, loc, name, started, favorite, deleted, channel in rows:
            db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                          source_cursor,last_sync_ms,run_json,favorite,deleted_ms,channel)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (bid, bid, loc, name, "lab", started, "completed", 0, started,
                        json.dumps({"arm": channel or "A"}), favorite, deleted, channel))
        db.commit()
    return store


def test_view_and_total():
    store = make(tempfile.mkdtemp())
    data = store.list_filtered()
    assert data["total"] == 3, data["total"]              # trash 默认不出现
    assert [b["batch_id"] for b in data["batches"]] == ["b4", "b2", "b1"]
    assert store.list_filtered(view="trash")["total"] == 1
    assert store.list_filtered(view="favorites")["total"] == 2
    print("PASS 视图过滤：all=3 / trash=1 / favorites=2")


def test_query_location_and_name():
    store = make(tempfile.mkdtemp())
    assert [b["batch_id"] for b in store.list_filtered(query="实验室A")["batches"]] == ["b2"]
    assert [b["batch_id"] for b in store.list_filtered(query="收藏测试")["batches"]] == ["b4"]
    assert [b["batch_id"] for b in store.list_filtered(query="夜间", view="trash")["batches"]] == ["b3"]
    assert store.list_filtered(query="不存在的名字")["total"] == 0
    print("PASS 关键词：地点与批次名都能命中")


def test_channel_and_time():
    store = make(tempfile.mkdtemp())
    assert [b["batch_id"] for b in store.list_filtered(channel="C")["batches"]] == ["b1"]
    assert [b["batch_id"] for b in store.list_filtered(channel="A")["batches"]] == ["b4"]
    assert [b["batch_id"] for b in store.list_filtered(channel="D")["batches"]] == ["b2"]
    data = store.list_filtered(since_ms=T0 + 3600_000, until_ms=T0 + 7200_000)
    assert [b["batch_id"] for b in data["batches"]] == ["b2"], data["batches"]
    print("PASS 通道与时间窗：C/D 与 since/until 均正确")


def test_pagination_has_more():
    store = make(tempfile.mkdtemp())
    page1 = store.list_filtered(limit=2, offset=0)
    page2 = store.list_filtered(limit=2, offset=2)
    assert page1["total"] == 3 and page1["has_more"] is True
    assert page2["has_more"] is False
    ids = [b["batch_id"] for b in page1["batches"]] + [b["batch_id"] for b in page2["batches"]]
    assert ids == ["b4", "b2", "b1"], ids
    print("PASS 分页：limit/offset 拼接无重无漏，has_more 正确")


def test_rejects_bad_params():
    store = make(tempfile.mkdtemp())
    for kwargs in ({"limit": 0}, {"limit": 1001}, {"offset": -1}, {"view": "weird"}):
        try:
            store.list_filtered(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝 {kwargs}")
    print("PASS 参数校验：limit/offset/view 非法值被拒绝")


if __name__ == "__main__":
    test_view_and_total()
    test_query_location_and_name()
    test_channel_and_time()
    test_pagination_has_more()
    test_rejects_bad_params()
