"""Persistent non-overlapping environment windows; independent of vision runs.

P1-04：改间隔前必须把积压**按旧间隔**补齐（可分批），只允许最后不足一个旧周期的残段标 partial。
P1-05：历史接口支持三级游标（最新 / after_id 增量 / before_id 回溯）与 since/until 过滤。
"""
import asyncio
from contextlib import closing
import json
import math
import sqlite3
import time


class LongTerm:
    def __init__(self, path, clock=None):
        self.path = str(path)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.error = None
        with closing(self.connect()) as db:
            db.executescript('''CREATE INDEX IF NOT EXISTS idx_meas_received_longterm ON measurements(received_at_ms);
            CREATE TABLE IF NOT EXISTS longterm_config(id INTEGER PRIMARY KEY CHECK(id=1),interval_s INTEGER,window_start_ms INTEGER,revision INTEGER,changed_ms INTEGER);
            CREATE TABLE IF NOT EXISTS longterm_windows(id INTEGER PRIMARY KEY,start_ms INTEGER,end_ms INTEGER,interval_s INTEGER,revision INTEGER,partial INTEGER,computed_ms INTEGER,json TEXT);
            CREATE TABLE IF NOT EXISTS longterm_changes(revision INTEGER PRIMARY KEY,interval_s INTEGER,effective_ms INTEGER);''')
            now = self.clock()
            db.execute('INSERT OR IGNORE INTO longterm_config VALUES(1,60,?,1,?)', (now, now))
            db.execute('INSERT OR IGNORE INTO longterm_changes SELECT revision,interval_s,changed_ms FROM longterm_config')
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _window(self, db, c, end):
        stats = {}
        for row in db.execute('SELECT sensor_id,zone_id,quality,received_at_ms,values_json FROM measurements '
                              'WHERE received_at_ms>=? AND received_at_ms<? ORDER BY received_at_ms',
                              (c['window_start_ms'], end)):
            key = row['zone_id'] + '/' + row['sensor_id']
            s = stats.setdefault(key, {'samples': 0, 'invalid_samples': 0, 'last_received_ms': None, 'fields': {}})
            s['samples'] += 1
            s['last_received_ms'] = row['received_at_ms']
            if row['quality'] != 'ok':
                s['invalid_samples'] += 1
                continue
            for field, v in json.loads(row['values_json']).items():
                if type(v) not in (int, float) or not math.isfinite(v):
                    continue
                a = s['fields'].setdefault(field, {'count': 0, 'sum': 0, 'min': v, 'max': v})
                a['count'] += 1
                a['sum'] += v
                a['min'] = min(a['min'], v)
                a['max'] = max(a['max'], v)
        for s in stats.values():
            for a in s['fields'].values():
                a['mean'] = a.pop('sum') / a['count']
        now = self.clock()
        partial = end - c['window_start_ms'] != c['interval_s'] * 1000
        db.execute('INSERT INTO longterm_windows(start_ms,end_ms,interval_s,revision,partial,computed_ms,json) '
                   'VALUES(?,?,?,?,?,?,?)',
                   (c['window_start_ms'], end, c['interval_s'], c['revision'], int(partial), now, json.dumps(stats)))
        db.execute('UPDATE longterm_config SET window_start_ms=? WHERE id=1', (end,))

    def _drain(self, batches=10000, batch_size=200):
        """把已到结算时间的窗口按当前间隔全部补齐；返回补了多少段。

        P1-04 的关键：configure 不能只 tick 一次（200 段上限）就把剩下的积压并成一段。
        """
        made = 0
        for _ in range(batches):
            progressed = 0
            with closing(self.connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                for _ in range(batch_size):
                    c = db.execute('SELECT * FROM longterm_config').fetchone()
                    end = c['window_start_ms'] + c['interval_s'] * 1000
                    if end > self.clock() - 2000:
                        break
                    self._window(db, c, end)
                    progressed += 1
                db.commit()
            made += progressed
            if not progressed:
                break
        return made

    def tick(self):
        self._drain(batches=1, batch_size=200)

    def configure(self, seconds):
        if type(seconds) is not int or not 5 <= seconds <= 86400 or seconds % 5:
            raise ValueError('记录间隔须为 5–86400 秒，且为 5 秒的整数倍')
        # 先把积压按旧间隔补齐（分批，但必须补完），再切换。
        self._drain()
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            c = db.execute('SELECT * FROM longterm_config').fetchone()
            now = self.clock()
            if c['interval_s'] == seconds:
                db.rollback()
                return self.settings()
            if now < c['window_start_ms']:
                db.rollback()
                raise ValueError('系统时钟发生回退，请稍后重试')
            # 此时剩余间隔不足一个旧周期；这段是允许的 partial 残段。
            if now > c['window_start_ms']:
                self._window(db, c, now)
            db.execute('UPDATE longterm_config SET interval_s=?,window_start_ms=?,revision=revision+1,changed_ms=? WHERE id=1',
                       (seconds, now, now))
            db.execute('INSERT INTO longterm_changes VALUES(?,?,?)', (c['revision'] + 1, seconds, now))
            db.commit()
        return self.settings()

    def settings(self):
        with closing(self.connect()) as db:
            c = dict(db.execute('SELECT * FROM longterm_config').fetchone())
        c.update(default_interval_s=60, min_interval_s=5, max_interval_s=86400, step_s=5,
                 next_end_ms=c['window_start_ms'] + c['interval_s'] * 1000, error=self.error,
                 time_basis='gateway_received_at_ms', raw_sampling_unchanged=True)
        return c

    def history(self, after_id=None, before_id=None, limit=200, since_ms=None, until_ms=None):
        """三个方向一致的契约：

        - 都不给：返回**最新** limit 段（升序），prev_before_id 用于继续往前翻；
        - after_id：增量拉新（升序，has_more 表示还有更新的）；
        - before_id：往前翻历史（升序，has_more 表示还有更早的）。
        """
        for name, value in (('after_id', after_id), ('before_id', before_id)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Invalid ' + name)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid limit')
        where, args = [], []
        if after_id is not None:
            where.append('id>?')
            args.append(after_id)
        if before_id is not None:
            where.append('id<?')
            args.append(before_id)
        if since_ms is not None:
            where.append('end_ms>=?')
            args.append(int(since_ms))
        if until_ms is not None:
            where.append('start_ms<=?')
            args.append(int(until_ms))
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        mode = 'latest' if (after_id is None and before_id is None) else ('after' if after_id is not None else 'before')
        descending = mode != 'after'          # 最新一批和往前翻都用 DESC 取最近 limit 条
        order = 'DESC' if descending else 'ASC'
        with closing(self.connect()) as db:
            rows = db.execute(f'SELECT * FROM longterm_windows{clause} ORDER BY id {order} LIMIT ?',
                              (*args, limit + 1)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM longterm_windows').fetchone()[0]
        has_more = len(rows) > limit
        rows = rows[:limit]
        if descending:
            rows = list(reversed(rows))
        data = []
        for row in rows:
            r = dict(row)
            r['sensors'] = json.loads(r.pop('json'))
            data.append(r)
        ids = [r['id'] for r in data]
        return {'rows': data, 'total': total, 'limit': limit, 'has_more': has_more, 'mode': mode,
                'next_after_id': max(ids) if ids else after_id,
                'prev_before_id': min(ids) if ids else before_id,
                'now_ms': self.clock(), 'settings': self.settings()}


def install(app, path):
    from aiohttp import web

    store = LongTerm(path)

    async def handler(request):
        if request.method == 'POST' and request.remote not in ('127.0.0.1', '::1'):
            return web.json_response({'error': 'settings require local authenticated proxy'}, status=403)
        try:
            loop = asyncio.get_running_loop()
            if request.method == 'POST':
                body = await request.json()
                data = await loop.run_in_executor(None, store.configure, body.get('interval_s'))
            elif request.path.endswith('history'):
                q = request.query

                def arg(name):
                    value = q.get(name)
                    return None if value in (None, '') else int(value)

                data = await loop.run_in_executor(None, lambda: store.history(
                    after_id=arg('after_id'), before_id=arg('before_id'),
                    limit=arg('limit') if arg('limit') is not None else 200,
                    since_ms=arg('since_ms'), until_ms=arg('until_ms')))
            else:
                data = await loop.run_in_executor(None, store.settings)
            return web.json_response(data, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, AttributeError) as e:
            return web.json_response({'error': str(e)}, status=400)

    async def lifecycle(app):
        async def work():
            while True:
                try:
                    await asyncio.get_running_loop().run_in_executor(None, store.tick)
                    store.error = None
                except Exception as e:  # noqa: BLE001 - 汇总失败不能拖垮网关
                    store.error = str(e)
                await asyncio.sleep(1)
        task = asyncio.create_task(work())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/env/v1/longterm/settings', handler)
    app.router.add_post('/env/v1/longterm/settings', handler)
    app.router.add_get('/env/v1/longterm/history', handler)
