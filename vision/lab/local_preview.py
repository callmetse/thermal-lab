"""Loopback aiohttp preview service; Caddy owns LAN access and authentication."""
import asyncio
from collections import OrderedDict
import hashlib
import json
import struct
import threading

from aiohttp import web


class LocalPreview:
    def __init__(self, host='127.0.0.1', port=28990, cache_frames=64, dataset_api_url=None,
                 preview_only=False, archive_client=None):
        if host != '127.0.0.1':
            raise ValueError('Preview must stay behind the authenticated loopback proxy')
        self.host, self.port = host, port
        # P1-06：配置了 dataset_api_url 就只做预览，批次库/归档/上传/数据页交给独立进程。
        self.dataset_api_url = dataset_api_url.rstrip('/') if dataset_api_url else None
        self.preview_only = preview_only
        self.archive_client = archive_client
        self.cache_frames = max(1, cache_frames)
        self.cache = OrderedDict()
        self.clients = {}
        self.run_id = None
        self.run_generation = 0
        self.last_frame = -1
        self.loop = None
        self.thread = None
        self.ready = threading.Event()
        self.error = None
        self.batch_store=None
        self.archive=None
        self.active_batch_id=None
        self.batch_capture_error=None
        self.batch_task=None
        self.session_run_id=None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='local-preview')
        self.thread.start()
        if not self.ready.wait(10):
            raise TimeoutError('Local preview startup timed out')
        if self.error:
            raise self.error
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as error:
            self.error = error
            self.ready.set()
            self.loop.close()
            return
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    async def _start(self):
        app = web.Application(client_max_size=512*1024*1024)
        if not self.dataset_api_url and not self.preview_only:
            # 兼容旧部署（未拆服务）：数据面内嵌在本进程
            from .video_uploads import install as install_uploads
            install_uploads(self, app)
            from pathlib import Path
            import os
            from .collection_routes import install,sync_loop
            source=Path(os.environ.get('ABC_ENV_DB','/home/pi/env_sensor_v1/env_sensor.sqlite3'))
            if source.exists():
                from .collection_batches import CollectionBatches
                self.batch_store=CollectionBatches(Path(__file__).resolve().parent.parent/'data/collection_batches.sqlite3',source)
            if self.batch_store:
                from .experiment_archive import ExperimentArchive
                self.archive=ExperimentArchive(self.batch_store,Path(__file__).resolve().parent.parent)
            install(self,app)
            self.batch_task=asyncio.create_task(sync_loop(self))
        elif not self.preview_only:
            # R2：拆分模式下 worker 仍然调 archive.submit()，这里给一个有界投递入口，
            # 后台批量送进数据面，实时链路只做入队。
            from .archive_ingest import ArchiveIngest
            self.archive=ArchiveIngest(self.dataset_api_url)
        app.router.add_get('/live-video', self._websocket)
        for path in ('/api/preview', '/api/preview-frame', '/api/preview-meta'):
            app.router.add_get(path, self._get)
        app.router.add_get('/health', self._health)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def _call(self, coroutine, timeout=5):
        """跨线程调用 loop；超时必须显式取消，不能让迟到的结果继续改状态。"""
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError('本地预览服务 %.1f s 未响应（已取消该操作）' % timeout) from None

    def set_run(self, run_id, context=None, timeout=5):
        self.run_generation += 1
        generation = self.run_generation
        # 数据面独立时，批次生命周期由 dataset-api 负责，预览只改自己的 run_id/缓存。
        self._call(self._set_run(generation, run_id, context,
                                 not self.dataset_api_url and not self.preview_only), timeout=timeout)
        if self.dataset_api_url and not self.preview_only:
            # Compatibility entry point for standalone callers. The deployed worker owns its client.
            if run_id is not None:
                self.archive.start_session({**(context or {}), 'run_id': run_id})
                self.session_run_id = run_id
            elif self.session_run_id:
                self.archive.finish_session(self.session_run_id)
                self.session_run_id = None

    async def _set_run(self, generation, run_id, context=None, manage_batches=True):
        if generation != self.run_generation:
            return False
        loop = asyncio.get_running_loop()
        created_batch = None
        try:
            if manage_batches and self.batch_store is not None:
                try:
                    if self.active_batch_id:
                        archive_error=None
                        if self.archive:
                            try:
                                summary=await loop.run_in_executor(None,self.archive.summary,self.active_batch_id) or {}
                                archive_error=summary.get('error')
                            except Exception:  # noqa: BLE001 - 归档查询失败不阻塞批次结算
                                archive_error='archive_summary_unavailable'
                        await loop.run_in_executor(None,self.batch_store.finish,self.active_batch_id,None,archive_error)
                        self.active_batch_id=None
                    if run_id:
                        run=dict(context or {'run_id':run_id})
                        created_batch=await loop.run_in_executor(None,self.batch_store.start,run)
                        if self.archive:await loop.run_in_executor(None,self.archive.begin,run)
                        if generation != self.run_generation:
                            # 迟到：把已建批次显式收尾，绝不把旧 run 挂成活动批次。
                            loop.run_in_executor(None,self.batch_store.finish,created_batch,None,'superseded_by_newer_run')
                            return False
                        self.active_batch_id=created_batch
                        self.batch_capture_error=None
                except Exception as e:self.batch_capture_error=type(e).__name__+': '+str(e)
            if generation != self.run_generation:
                return False
            self.run_id, self.last_frame = run_id, -1
            self.cache.clear()
            for queue in self.clients.values():
                while not queue.empty():
                    queue.get_nowait()
            return True
        except asyncio.CancelledError:
            # 启动被取消/超时：已创建的批次不能变成幽灵批次。
            if created_batch is not None:
                loop.run_in_executor(None,self.batch_store.finish,created_batch,None,'start_cancelled')
            raise

    def publish(self, packet):
        return self._call(self._publish(packet))

    async def _publish(self, packet):
        if len(packet) < 6:
            raise ValueError('Truncated preview')
        size = struct.unpack('!I', packet[:4])[0]
        if size > 65536 or size + 4 >= len(packet):
            raise ValueError('Invalid preview header')
        meta = json.loads(packet[4:4+size])
        frame = meta.get('source_frame_id')
        if self.run_id is None or meta.get('run_id') != self.run_id:
            return False
        if type(frame) is not int or frame <= self.last_frame:
            return False
        identity = self.run_id + ':' + str(frame)
        if meta.get('preview_id') != identity:
            raise ValueError('Preview identity mismatch')
        jpeg = packet[4+size:]
        if hashlib.sha256(jpeg).hexdigest() != meta.get('preview_jpeg_sha256'):
            raise ValueError('Preview JPEG hash mismatch')
        obs = meta.get('observation') or {}
        if (obs.get('run_id') != self.run_id or obs.get('source_frame_id') != frame
                or obs.get('jpeg_sha256') != meta.get('jpeg_sha256')):
            raise ValueError('Preview observation mismatch')
        self.cache[identity] = (packet, meta, jpeg)
        self.last_frame = frame
        while len(self.cache) > self.cache_frames:
            self.cache.popitem(last=False)
        for queue in self.clients.values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(packet)
        return True

    async def _get(self, request):
        requested = request.query.get('v')
        identity = requested or next(reversed(self.cache), None)
        entry = self.cache.get(identity)
        headers = {'Cache-Control': 'no-store'}
        if not entry or self.run_id is None:
            return web.json_response({'error': 'Frame expired'}, status=410 if requested else 404, headers=headers)
        packet, meta, jpeg = entry
        headers['X-Preview-Id'] = identity
        if request.path == '/api/preview-meta':
            return web.json_response(meta, headers=headers)
        return web.Response(body=jpeg if request.path == '/api/preview' else packet,
                            content_type='image/jpeg' if request.path == '/api/preview' else 'application/octet-stream',
                            headers=headers)

    async def _health(self, request):
        payload = {'run_id': self.run_id, 'frames_cached': len(self.cache),
                   'clients': len(self.clients), 'transport': 'pi-local',
                   'active_batch_id': self.active_batch_id,
                   'batch_capture_error': self.batch_capture_error,
                   'dataset_api': self.dataset_api_url or getattr(self.archive_client, 'url', None)}
        archive = self.archive_client or self.archive
        stats = getattr(archive, 'stats', None)
        if isinstance(stats, dict):
            payload['archive_ingest'] = dict(stats)
            payload['archive_error'] = getattr(archive, 'error', None)
            payload['active_batch_id'] = getattr(archive, 'active_batch_id', self.active_batch_id)
        return web.json_response(payload)

    async def _websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=1)
        self.clients[ws] = queue
        if self.cache:
            queue.put_nowait(next(reversed(self.cache.values()))[0])
        async def send():
            try:
                while True:
                    packet = await queue.get()
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=2)
            except (TimeoutError, ConnectionError, RuntimeError):
                await ws.close()
        sender = asyncio.create_task(send())
        try:
            async for _ in ws:
                pass
        finally:
            self.clients.pop(ws, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def _shutdown(self):
        await self._set_run(self.run_generation, None)
        if self.batch_task:
            self.batch_task.cancel()
            await asyncio.gather(self.batch_task,return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in list(self.clients)), return_exceptions=True)
        await self.runner.cleanup()
        if self.archive:
            try:
                await asyncio.get_running_loop().run_in_executor(None,self.archive.close)
            except Exception:  # noqa: BLE001 - 归档关闭失败不能拖住停机
                self.batch_capture_error = 'archive_close_failed' 

    def close(self, timeout=10):
        if self.thread and self.thread.is_alive():
            try:
                self._call(self._shutdown(), timeout=timeout)
            except TimeoutError:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
