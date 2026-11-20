"""Loopback aiohttp preview service; Caddy owns LAN access and authentication."""
import asyncio
from collections import OrderedDict
import hashlib
import json
import struct
import threading

from aiohttp import web


class LocalPreview:
    def __init__(self, host='127.0.0.1', port=28990, cache_frames=64):
        if host != '127.0.0.1':
            raise ValueError('Preview must stay behind the authenticated loopback proxy')
        self.host, self.port = host, port
        self.cache_frames = max(1, cache_frames)
        self.cache = OrderedDict()
        self.clients = {}
        self.run_id = None
        self.last_frame = -1
        self.loop = None
        self.thread = None
        self.ready = threading.Event()
        self.error = None

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
        app = web.Application()
        app.router.add_get('/live-video', self._websocket)
        for path in ('/api/preview', '/api/preview-frame', '/api/preview-meta'):
            app.router.add_get(path, self._get)
        app.router.add_get('/health', self._health)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=5)

    def set_run(self, run_id):
        self._call(self._set_run(run_id))

    async def _set_run(self, run_id):
        self.run_id, self.last_frame = run_id, -1
        self.cache.clear()
        for queue in self.clients.values():
            while not queue.empty():
                queue.get_nowait()

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
        return web.json_response({'run_id': self.run_id, 'frames_cached': len(self.cache),
                                  'clients': len(self.clients), 'transport': 'pi-local'})

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
        await self._set_run(None)
        await asyncio.gather(*(ws.close() for ws in list(self.clients)), return_exceptions=True)
        await self.runner.cleanup()

    def close(self):
        if self.thread and self.thread.is_alive():
            self._call(self._shutdown())
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
