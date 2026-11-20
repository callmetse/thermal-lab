"""env_sensor_v1 网关：接收 ESP32 的 HTTP 上报，校验、去重、落盘，并提供只读查询。

独立进程、独立端口；不导入、不修改 ABC 应用（lab.worker / 视觉与声学链路）。
ESP32 每 2 秒一条温湿度、每 5 秒一条 CO₂，都是小 JSON POST，不需要 broker。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env_schema as S  # noqa: E402
import env_store  # noqa: E402

TOKEN_HEADER = 'X-Env-Token'
CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, ' + TOKEN_HEADER,
}


def _json(data, status=200):
    return web.json_response(data, status=status, headers=CORS)


class Gateway:
    def __init__(self, store, token=None):
        self.store = store
        self.token = token or ''

    def _authorized(self, request):
        if not self.token:
            return True
        return request.headers.get(TOKEN_HEADER, '') == self.token

    async def post_measurement(self, request):
        if not self._authorized(request):
            return _json({'ok': False, 'error': 'unauthorized'}, 401)
        try:
            raw = await request.json()
        except Exception:
            return _json({'ok': False, 'error': 'body must be JSON'}, 400)
        now = self.store.now_ms()
        try:
            event = S.validate_measurement(raw, now_ms=now)
        except S.SchemaError as exc:
            print('[env] reject: %s' % exc, flush=True)
            return _json({'ok': False, 'error': str(exc)}, 400)

        inserted = self.store.insert(event, received_at_ms=now)
        print('[env] %s %s seq=%d %s%s'
              % (event['device_id'], event['sensor_id'], event['seq'],
                 json.dumps(event['values'], ensure_ascii=False),
                 '' if inserted else ' (duplicate)'), flush=True)
        return _json({'ok': True, 'duplicate': not inserted,
                      'event_id': event['event_id'],
                      'quality': event['quality']}, 201 if inserted else 200)

    async def post_status(self, request):
        if not self._authorized(request):
            return _json({'ok': False, 'error': 'unauthorized'}, 401)
        try:
            raw = await request.json()
        except Exception:
            return _json({'ok': False, 'error': 'body must be JSON'}, 400)
        now = self.store.now_ms()
        try:
            status = S.validate_status(raw, now_ms=now)
        except S.SchemaError as exc:
            print('[env] status reject: %s' % exc, flush=True)
            return _json({'ok': False, 'error': str(exc)}, 400)
        inserted = self.store.insert_status(status, received_at_ms=now)
        return _json({'ok': True, 'duplicate': not inserted,
                      'event_id': status['event_id']}, 201 if inserted else 200)

    async def get_latest(self, request):
        zone = request.query.get('zone_id', 'lab')
        try:
            S._ident(zone, 'zone_id')
        except S.SchemaError as exc:
            return _json({'ok': False, 'error': str(exc)}, 400)
        return _json(self.store.latest(zone))

    async def get_history(self, request):
        zone = request.query.get('zone_id', 'lab')
        field = request.query.get('field') or None
        try:
            S._ident(zone, 'zone_id')
            since = request.query.get('since_ms')
            until = request.query.get('until_ms')
            minutes = request.query.get('minutes')
            limit = int(request.query.get('limit', 1000))
        except (S.SchemaError, ValueError) as exc:
            return _json({'ok': False, 'error': str(exc)}, 400)

        now = self.store.now_ms()
        try:
            since_ms = int(since) if since else (
                now - int(float(minutes) * 60_000) if minutes else now - 3_600_000)
            until_ms = int(until) if until else now
            if since_ms > until_ms or not 1 <= limit <= 10000:
                raise ValueError('invalid time window or limit (1..10000)')
            rows = self.store.history(zone, since_ms, until_ms, field, limit)
        except (ValueError, OverflowError) as exc:
            return _json({'ok': False, 'error': str(exc)}, 400)
        return _json({'zone_id': zone, 'since_ms': since_ms, 'until_ms': until_ms,
                      'field': field, 'count': len(rows), 'rows': rows})

    async def health(self, request):
        stats = self.store.stats()
        return _json({'ok': True, 'now_ms': self.store.now_ms(),
                      'schema_version': S.SCHEMA_VERSION,
                      'auth': bool(self.token), 'stats': stats,
                      'stale_after_ms': S.STALE_AFTER_MS})

    async def preflight(self, request):
        return web.Response(status=204, headers=CORS)


def build_app(db_path, token=None):
    store = env_store.EnvStore(db_path)
    gateway = Gateway(store, token)
    app = web.Application(client_max_size=64 * 1024)
    app.add_routes([
        web.post('/env/v1/measurements', gateway.post_measurement),
        web.post('/env/v1/status', gateway.post_status),
        web.get('/env/v1/latest', gateway.get_latest),
        web.get('/env/v1/history', gateway.get_history),
        web.get('/env/v1/health', gateway.health),
        web.options('/{tail:.*}', gateway.preflight),
    ])
    import env_longterm
    env_longterm.install(app, db_path)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='env_sensor_v1 网关')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=28984)
    parser.add_argument('--db', default=str(Path(__file__).with_name('env_sensor.sqlite3')))
    parser.add_argument('--token', default=os.environ.get('ENV_SENSOR_TOKEN', ''))
    parser.add_argument('--retention-days', type=float, default=30.0)
    args = parser.parse_args(argv)

    store = env_store.EnvStore(args.db)
    if args.retention_days > 0:
        removed = store.prune(store.now_ms() - int(args.retention_days * 86_400_000))
        if removed:
            print('[env] pruned %d rows older than %.0f days'
                  % (removed, args.retention_days), flush=True)

    app = build_app(args.db, args.token)
    print('[env] listening on http://%s:%d db=%s token=%s'
          % (args.host, args.port, args.db, 'on' if args.token else 'off'), flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == '__main__':
    main()
