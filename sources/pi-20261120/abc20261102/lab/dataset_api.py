"""独立数据面服务（P1-06）：批次库/归档/上传/环境曲线与视觉进程解耦。

`vision-edge` 只保留实时预览；数据页 `/data`、上传 `/api/video-uploads`、批次列表与详情、
导出、收藏/回收站、环境曲线全部由本服务在 28992 提供。本进程不加载摄像头/模型，
所以视觉进程崩溃、重启、升级都不再影响数据面。

启动：`python -m lab.dataset_api`
会话边界由 worker 通过 `POST /api/dataset/session` 通知（`config.json` 里 `dataset_api: true` 时）。

重启安全：启动时会把数据库里仍是 `active` 的批次接管为当前批次，避免"内存空了、
库里还挂着 active"的幽灵状态；下次会话结束时照常收尾。
"""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
from sqlite3 import IntegrityError, OperationalError
import threading

from aiohttp import web

from . import video_uploads
from .collection_batches import CollectionBatches
from .collection_routes import install as install_collection_routes, sync_loop
from .experiment_archive import ExperimentArchive
from .video_uploads import install as install_uploads

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 28992


class StaleGeneration(ValueError):
    """会话代次过期（R4）：旧 start/stop 不得影响当前批次。"""


class SessionConflict(ValueError):
    """会话冲突（R4）：重复 run_id 不得结算当前批次。"""


class DatasetApi:
    def __init__(self, host='127.0.0.1', port=DEFAULT_PORT, root=None, env_db=None):
        if host != '127.0.0.1':
            raise ValueError('Dataset API must stay behind the authenticated loopback proxy')
        self.host, self.port = host, port
        self.root = Path(root) if root is not None else ROOT
        self.env_db = Path(env_db) if env_db is not None else Path(
            os.environ.get('ABC_ENV_DB', '/home/pi/env_sensor_v1/env_sensor.sqlite3'))
        self.batch_store = None
        self.archive = None
        self.active_batch_id = None
        self.batch_capture_error = None
        self.active_run_id = None
        self.session_generation = None
        self.adopted_on_restart = False
        self.loop = None
        self.thread = None
        self.ready = threading.Event()
        self.error = None
        self.session_lock = None
        self.records_lock = None
        self.batch_task = None
        self.runner = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='dataset-api')
        self.thread.start()
        if not self.ready.wait(20):
            raise TimeoutError('dataset api startup timed out')
        if self.error:
            raise self.error
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as error:  # noqa: BLE001 - 启动失败要回传给 start()
            self.error = error
            self.ready.set()
            self.loop.close()
            return
        self.ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    async def _start(self):
        video_uploads.ROOT = self.root
        self.session_lock = asyncio.Lock()
        self.records_lock = asyncio.Lock()
        self.batch_store = CollectionBatches(self.root / 'data/collection_batches.sqlite3',
                                             self.env_db, mark_interrupted=False)
        self.archive = ExperimentArchive(self.batch_store, self.root)
        stale = self.batch_store.recover_active_batches('stale_active_on_restart', keep_latest=True)
        with closing(self.batch_store.connect()) as db:
            row = db.execute("SELECT batch_id,run_id,run_json FROM batches WHERE status='active' "
                             "ORDER BY started_ms DESC LIMIT 1").fetchone()
        if row:
            self.active_batch_id = row['batch_id']
            self.active_run_id = row['run_id']
            self.adopted_on_restart = True
            try:
                self.session_generation = json.loads(row['run_json']).get('generation')
            except (TypeError, ValueError):
                self.session_generation = None
        if stale or self.active_batch_id:
            print('dataset-api: 接管 active 批次 %s（更早残留 %d 条已标 interrupted）'
                  % (self.active_batch_id, stale), flush=True)
        app = web.Application(client_max_size=512 * 1024 * 1024)
        install_uploads(self, app)
        install_collection_routes(self, app)
        app.router.add_post('/api/dataset/session', self._session)
        app.router.add_post('/api/dataset/records', self._records)
        app.router.add_get('/api/dataset/state', self._state)
        app.router.add_get('/health', self._health)
        if self.batch_store is not None:
            self.batch_task = asyncio.create_task(sync_loop(self))
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def state(self):
        return {'active_batch_id': self.active_batch_id, 'active_run_id': self.active_run_id,
                'batch_capture_error': self.batch_capture_error,
                'archive_available': self.archive is not None,
                'session_generation': self.session_generation,
                'adopted_on_restart': self.adopted_on_restart}

    def _lookup_run(self, run_id):
        with closing(self.batch_store.connect()) as db:
            row = db.execute("SELECT batch_id,status FROM batches WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    async def _finish_active(self, loop, ingest_stats=None, execution_status=None, reason=None, ended_ms=None):
        archive_error = None
        if self.archive:
            try:
                summary = await loop.run_in_executor(None, self.archive.summary, self.active_batch_id) or {}
                archive_error = summary.get('error')
            except Exception:  # noqa: BLE001 - 归档查询失败不阻塞批次结算
                archive_error = 'archive_summary_unavailable'
        dropped = int((ingest_stats or {}).get('dropped') or 0)
        uncertain = bool((ingest_stats or {}).get('delivery_uncertain') or (ingest_stats or {}).get('backlog'))
        await loop.run_in_executor(None, self.batch_store.finish, self.active_batch_id, reason,
                                   archive_error, dropped, execution_status, uncertain, ended_ms)
        self.active_batch_id = None
        self.active_run_id = None

    def submit_session(self, run_id, context=None, generation=None, target_run_id=None, timeout=20):
        """跨线程同步入口（测试/运维脚本用）；HTTP 路由仍走 async set_session。"""
        if self.loop is None:
            raise RuntimeError('dataset api not started')
        future = asyncio.run_coroutine_threadsafe(
            self.set_session(run_id, context, generation, target_run_id), self.loop)
        return future.result(timeout=timeout)

    async def set_session(self, run_id, context=None, generation=None, target_run_id=None, ingest_stats=None,
                          execution_status=None, reason=None, started_ms=None, ended_ms=None):
        """会话边界。R4：代次单调、重复 start 幂等、停止必须指向当前会话。"""
        if self.batch_store is None:
            raise ValueError('批次库不可用（环境库不存在）')
        async with self.session_lock:
            loop = asyncio.get_running_loop()
            if run_id is None and target_run_id is not None and target_run_id != self.active_run_id:
                target = await loop.run_in_executor(None, self._lookup_run, target_run_id)
                if target and target['status'] != 'active':
                    return {**self.state(), 'generation': generation, 'action': 'already_finished'}
                raise SessionConflict('停止请求未指向当前活动会话')
            existing = await loop.run_in_executor(None, self._lookup_run, run_id) if run_id is not None else None
            if started_ms is not None and existing and existing['status'] != 'active':
                return {**self.state(), 'generation': generation, 'action': 'already_finished'}
            if (generation is not None and self.session_generation is not None
                    and generation < self.session_generation):
                raise StaleGeneration('过期会话代次 generation=%s（当前 %s）被拒绝'
                                      % (generation, self.session_generation))
            if run_id is None:
                if self.active_batch_id is None:
                    return {**self.state(), 'generation': generation, 'action': 'idle'}
                if target_run_id is not None and target_run_id != self.active_run_id:
                    raise SessionConflict('停止请求指向会话 %s，当前活动会话是 %s'
                                          % (target_run_id, self.active_run_id))
                await self._finish_active(loop, ingest_stats, execution_status, reason, ended_ms)
                return {**self.state(), 'generation': generation, 'action': 'finished'}
            if existing is not None:
                if existing['status'] == 'active':
                    # 同一 run_id 的重复 start：幂等返回，不结算、不重建
                    self.active_batch_id = existing['batch_id']
                    self.active_run_id = run_id
                    if self.archive and isinstance(context, dict):
                        await loop.run_in_executor(None, self.archive.begin, {**context,'run_id':run_id})
                    if generation is not None:
                        self.session_generation = generation
                    return {**self.state(), 'generation': generation, 'action': 'already_active'}
                raise SessionConflict('run_id %s 已存在且状态为 %s；重复 start 不重建批次，也不结算当前会话'
                                      % (run_id, existing['status']))
            if self.active_batch_id:
                await self._finish_active(loop, {'delivery_uncertain': True}, 'interrupted', 'superseded_by_new_run')
            run = dict(context or {'run_id': run_id})
            run['run_id'] = run_id
            if generation is not None:
                run['generation'] = generation
            batch_id = await loop.run_in_executor(None, self.batch_store.start, run, started_ms)
            self.active_batch_id = batch_id
            self.active_run_id = run_id
            self.batch_capture_error = None
            if generation is not None:
                self.session_generation = generation
            if self.archive:
                await loop.run_in_executor(None, self.archive.begin, run)
            return {**self.state(), 'generation': generation, 'action': 'started'}

    async def _records(self, request):
        """R2：视觉侧的归档入口。有界批次、去重由 experiment_records 的唯一键保证。"""
        from urllib.parse import urlsplit
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return web.json_response({'error': '不允许跨站修改'}, status=403)
        if request.content_type != 'application/json':
            return web.json_response({'error': '需要 JSON 请求'}, status=415)
        # Bound the body even for chunked requests, independently of the upload route's limit.
        limit = 8 * 1024 * 1024
        if request.content_length and request.content_length > limit:
            return web.json_response({'error': '请求过大'}, status=413)
        if self.records_lock.locked():
            return web.json_response({'error': '归档正在写入，请重试'}, status=503)
        if self.archive is None:
            return web.json_response({'error': '归档不可用'}, status=503)
        async with self.records_lock:
            try:
                raw = bytearray()
                async for chunk in request.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        return web.json_response({'error': '请求过大'}, status=413)
                body = json.loads(raw)
                records = body.get('records') if isinstance(body, dict) else None
                if not isinstance(records, list) or not 1 <= len(records) <= 500:
                    raise ValueError('需要 1–500 条记录')
                items = []
                for item in records:
                    if not isinstance(item, dict):
                        raise ValueError('记录格式错误')
                    kind, payload = item.get('kind'), item.get('body')
                    if (kind not in ('observation', 'event', 'environment') or not isinstance(payload, dict)
                            or not isinstance(payload.get('run_id'), str)
                            or not isinstance(payload.get('collection_batch_id') or payload.get('run_id'), str)):
                        raise ValueError('记录类型或内容错误')
                    items.append((kind, payload))
                async with self.session_lock:
                    receipt = await asyncio.to_thread(self.archive.persist_delivery, items)
                return web.json_response({**receipt, 'active_batch_id': self.active_batch_id})
            except KeyError as error:
                return web.json_response({'error': str(error)}, status=422)
            except (OSError, OperationalError) as error:
                return web.json_response({'error': type(error).__name__}, status=503)
            except (ValueError, TypeError) as error:
                return web.json_response({'error': str(error)}, status=400)

    async def _session(self, request):
        from urllib.parse import urlsplit
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return web.json_response({'error': '不允许跨站修改'}, status=403)
        if request.content_type != 'application/json':
            return web.json_response({'error': '需要 JSON 请求'}, status=415)
        if request.content_length and request.content_length > 128000:
            return web.json_response({'error': '请求过大'}, status=413)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('无效请求')
            for name in ('started_ms', 'ended_ms', 'generation'):
                value = body.get(name)
                if value is not None and (type(value) is not int or not -(2**63) < value < 2**63):
                    raise ValueError('无效会话字段 '+name)
            status = body.get('execution_status')
            if status not in (None, 'completed', 'failed', 'interrupted'):
                raise ValueError('无效执行状态')
            if body.get('ended_ms') is not None and not body.get('target_run_id'):
                raise ValueError('停止请求必须指定会话')
            result = await self.set_session(body.get('run_id'), body.get('context'), body.get('generation'),
                                            body.get('target_run_id'), body.get('ingest_stats'), status,
                                            body.get('ended_reason'), body.get('started_ms'), body.get('ended_ms'))
            return web.json_response(result)
        except (StaleGeneration, SessionConflict) as error:
            return web.json_response({'error': str(error)}, status=409)
        except (ValueError, TypeError) as error:
            return web.json_response({'error': str(error)}, status=400)
        except IntegrityError as error:
            # run_id 是唯一键：重复的会话启动命令应得到明确 409，而不是 500 + 堆栈
            return web.json_response({'error': '会话启动冲突：%s（run_id 已被占用，请换一个或先收尾旧批次）' % error},
                                     status=409)

    async def _state(self, request):
        return web.json_response(self.state(), headers={'Cache-Control': 'no-store'})

    async def _health(self, request):
        return web.json_response({'transport': 'pi-local-dataset', 'port': self.port,
                                  'environment_db_present': self.env_db.is_file(), **self.state()})

    async def _shutdown(self):
        # 故意不结束活动批次：会话归 worker，服务重启不该把正在采集的实验判结束。
        if self.batch_task:
            self.batch_task.cancel()
            await asyncio.gather(self.batch_task, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self.archive:
            await asyncio.get_running_loop().run_in_executor(None, self.archive.close)

    def stop(self):
        if self.thread and self.thread.is_alive():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            try:
                future.result(timeout=10)
            except Exception:  # noqa: BLE001 - 停机路径尽力而为
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立数据面服务（批次库/归档/上传/环境曲线）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=int(os.environ.get('ABC_DATASET_API_PORT', DEFAULT_PORT)))
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--env-db', default=None)
    args = parser.parse_args(argv)
    api = DatasetApi(host=args.host, port=args.port, root=args.root, env_db=args.env_db).start()
    print('dataset-api listening on %s:%d (root=%s)' % (api.host, api.port, api.root), flush=True)
    try:
        api.thread.join()
    except KeyboardInterrupt:
        api.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
