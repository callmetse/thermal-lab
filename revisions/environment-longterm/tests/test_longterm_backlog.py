"""P1-04 / P1-05 回归：积压补齐与历史分页。

用临时 SQLite + 可推进的模拟时钟，不碰现场数据。
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env_longterm import LongTerm  # noqa: E402

T0 = 1_800_000_000_000  # 固定起点，避免依赖真实时间


def make_db(path, start=T0, rows=0, step_ms=2000):
    db = sqlite3.connect(str(path))
    db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, sensor_id TEXT, zone_id TEXT,
                        quality TEXT, received_at_ms INTEGER, values_json TEXT);''')
    for i in range(rows):
        db.execute('INSERT INTO measurements(sensor_id,zone_id,quality,received_at_ms,values_json) VALUES(?,?,?,?,?)',
                   ('sht4x', 'lab', 'ok', start + i * step_ms, json.dumps({'temperature_c': 20 + i % 3})))
    db.commit()
    db.close()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def contiguity(windows):
    for prev, cur in zip(windows, windows[1:]):
        assert prev['end_ms'] == cur['start_ms'], f"不连续: {prev['end_ms']} -> {cur['start_ms']}"


def test_one_hour_backlog_reconfigure():
    tmp = Path(tempfile.mkdtemp()) / "lt1.sqlite3"
    make_db(tmp, rows=1800)          # 1 小时、2 秒一条
    clock = Clock(T0)
    lt = LongTerm(tmp, clock=clock)
    lt.configure(5)                  # 切到 5 秒
    clock.now = T0 + 3600 * 1000 + 2000   # 积压 1 小时
    settings = lt.configure(60)
    with sqlite3.connect(str(tmp)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM longterm_windows ORDER BY id')]
    assert rows, "没有生成任何窗口"
    contiguity(rows)
    assert rows[-1]['end_ms'] == clock.now, (rows[-1]['end_ms'], clock.now)
    long_old = [r for r in rows if r['interval_s'] == 5 and r['end_ms'] - r['start_ms'] > 5000]
    assert not long_old, f"旧间隔窗口被拉长: {long_old[:2]}"
    assert settings['interval_s'] == 60
    assert settings['window_start_ms'] == clock.now
    print(f"PASS 1h 积压：{len(rows)} 段，无超长旧窗口，末段止于切换点")


def test_two_hour_backlog_24h_scale():
    tmp = Path(tempfile.mkdtemp()) / "lt2.sqlite3"
    make_db(tmp, rows=200)
    clock = Clock(T0)
    lt = LongTerm(tmp, clock=clock)
    lt.configure(5)
    # 24 小时积压：远超 tick 单次 200 段上限
    clock.now = T0 + 24 * 3600 * 1000 + 2000
    settings = lt.configure(60)
    with sqlite3.connect(str(tmp)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM longterm_windows ORDER BY id')]
    contiguity(rows)
    assert rows[-1]['end_ms'] == clock.now
    bad = [r for r in rows if r['interval_s'] == 5 and r['end_ms'] - r['start_ms'] > 5000]
    assert not bad, f"存在被合并的旧窗口: {bad[:2]}"
    assert rows[-1]['interval_s'] == 5 or rows[-1]['partial'] == 1
    assert settings['interval_s'] == 60
    print(f"PASS 24h 积压：{len(rows)} 段（超过 200 段上限仍全部补齐）")


def test_same_interval_is_noop():
    tmp = Path(tempfile.mkdtemp()) / "lt3.sqlite3"
    make_db(tmp, rows=10)
    clock = Clock(T0)
    lt = LongTerm(tmp, clock=clock)
    before = lt.settings()
    clock.now = T0 + 10_000
    after = lt.configure(before['interval_s'])
    with sqlite3.connect(str(tmp)) as db:
        count = db.execute('SELECT COUNT(*) FROM longterm_windows').fetchone()[0]
    assert after['revision'] == before['revision'], "相同间隔不应改修订号"
    assert count == 0, f"相同间隔不应写窗口: {count}"
    print("PASS 相同间隔：不写窗口、不升修订号")


def test_clock_rollback_rejected():
    tmp = Path(tempfile.mkdtemp()) / "lt4.sqlite3"
    make_db(tmp, rows=5)
    clock = Clock(T0)
    lt = LongTerm(tmp, clock=clock)
    lt.configure(30)
    clock.now = T0 - 60_000
    try:
        lt.configure(60)
    except ValueError as error:
        assert '回退' in str(error)
        print("PASS 时钟回退：拒绝改间隔")
        return
    raise AssertionError("时钟回退没有被拒绝")


def test_history_pagination():
    tmp = Path(tempfile.mkdtemp()) / "lt5.sqlite3"
    make_db(tmp, rows=10)
    clock = Clock(T0)
    lt = LongTerm(tmp, clock=clock)
    # 直接灌 3000 段，验证分页
    with sqlite3.connect(str(tmp)) as db:
        for i in range(3000):
            db.execute('INSERT INTO longterm_windows(start_ms,end_ms,interval_s,revision,partial,computed_ms,json) '
                       'VALUES(?,?,?,?,?,?,?)', (T0 + i * 5000, T0 + (i + 1) * 5000, 5, 1, 0, T0, '{}'))
        db.commit()

    latest = lt.history()
    assert len(latest['rows']) == 200 and latest['mode'] == 'latest'
    assert latest['has_more'] is True, "最新一批应该还有更早的"
    assert latest['rows'][-1]['id'] == 3000, latest['rows'][-1]['id']
    assert latest['rows'][0]['id'] == 2801, latest['rows'][0]['id']

    # 往前翻两页，拼起来必须无重无漏
    seen = [r['id'] for r in latest['rows']]
    cursor = latest['prev_before_id']
    while cursor and len(seen) < 3000:
        page = lt.history(before_id=cursor)
        ids = [r['id'] for r in page['rows']]
        assert ids == list(range(ids[0], ids[-1] + 1)), "单页内部不连续"
        assert set(ids).isdisjoint(seen), "翻页出现重复"
        seen = ids + seen
        cursor = page['prev_before_id'] if page['has_more'] else None
    assert seen == list(range(1, 3001)), f"翻完应为 1..3000，实际 {len(seen)} 条"
    print("PASS 历史回溯：3000 段无重无漏")

    # 增量：只看 id>2990
    inc = lt.history(after_id=2990)
    assert [r['id'] for r in inc['rows']] == [2991, 2992, 2993, 2994, 2995, 2996, 2997, 2998, 2999, 3000]
    assert inc['mode'] == 'after'
    print("PASS 增量：after_id 只返回更新的段")

    # 时间过滤
    window = lt.history(since_ms=T0 + 1000 * 5000, until_ms=T0 + 1003 * 5000)
    ids = [r['id'] for r in window['rows']]
    # 语义是“与 [since, until] 有重叠”，不是完全包含：id=i+1
    assert ids == [1000, 1001, 1002, 1003, 1004], ids
    print("PASS 时间过滤：since/until 生效")

    # 参数校验
    for bad in ({"after_id": -1}, {"before_id": "x"}, {"limit": 0}, {"limit": 5000}):
        try:
            lt.history(**bad)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝 {bad}")
    print("PASS 参数校验：非法游标/上限被拒绝")


if __name__ == "__main__":
    test_one_hour_backlog_reconfigure()
    test_two_hour_backlog_24h_scale()
    test_same_interval_is_noop()
    test_clock_rollback_rejected()
    test_history_pagination()
