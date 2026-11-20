import asyncio
import hashlib
import json
from pathlib import Path
import queue
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import ClientSession
from lab.common import pack
from lab.local_preview import LocalPreview
from lab.worker import Worker


def packet(frame, run='test', bad=False):
    jpeg = b'jpeg-' + str(frame).encode()
    obs = {'run_id': run, 'source_frame_id': frame, 'jpeg_sha256': 'source'}
    return pack({'project': 'abc20261102', 'run_id': run, 'source_frame_id': frame,
                 'preview_id': run + ':' + str(frame), 'jpeg_sha256': 'wrong' if bad else 'source',
                 'preview_jpeg_sha256': hashlib.sha256(jpeg).hexdigest(), 'observation': obs}, jpeg)


class LocalPreviewTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalPreview(port=0, cache_frames=2).start()
        self.url = 'http://127.0.0.1:' + str(self.server.port)
        self.server.set_run('test')

    def tearDown(self):
        self.server.close()
        self.assertFalse(self.server.thread.is_alive())

    def test_cache_identity_eviction_run_and_stop(self):
        self.assertTrue(self.server.publish(packet(1)))
        self.assertTrue(self.server.publish(packet(2)))
        self.assertFalse(self.server.publish(packet(1)))
        self.assertFalse(self.server.publish(packet(99, 'foreign')))
        with self.assertRaises(ValueError):
            self.server.publish(packet(3, bad=True))
        async def check():
            async with ClientSession() as client:
                async with client.get(self.url+'/api/preview-frame?v=test:1') as response:
                    self.assertEqual(await response.read(), packet(1))
                    self.assertEqual(response.headers['X-Preview-Id'], 'test:1')
                self.server.publish(packet(3))
                async with client.get(self.url+'/api/preview?v=test:1') as response:
                    self.assertEqual(response.status, 410)
                self.server.set_run(None)
                for suffix, status in [('',404), ('?v=test:3',410)]:
                    async with client.get(self.url+'/api/preview'+suffix) as response:
                        self.assertEqual(response.status, status)
                self.server.set_run('next')
                self.assertTrue(self.server.publish(packet(0,'next')))
        asyncio.run(check())

    def test_two_clients_and_reconnect_receive_exact_latest(self):
        async def check():
            async with ClientSession() as client:
                async with client.ws_connect(self.url+'/live-video') as a, client.ws_connect(self.url+'/live-video') as b:
                    self.server.publish(packet(1))
                    self.assertEqual((await a.receive(timeout=2)).data, packet(1))
                    self.assertEqual((await b.receive(timeout=2)).data, packet(1))
                self.server.publish(packet(2))
                async with client.ws_connect(self.url+'/live-video') as c:
                    self.assertEqual((await c.receive(timeout=2)).data, packet(2))
        asyncio.run(check())

    def test_waiting_queue_replaces_stale_frame(self):
        async def check_on_server():
            pending = asyncio.Queue(maxsize=1)
            self.server.clients['test'] = pending
            try:
                for frame in range(50):
                    await self.server._publish(packet(frame))
                self.assertEqual(pending.qsize(), 1)
                self.assertEqual(pending.get_nowait(), packet(49))
                self.assertEqual(len(self.server.cache), 2)
            finally:
                del self.server.clients['test']
        self.server._call(check_on_server())


class EdgePreviewTests(unittest.TestCase):
    def test_source_jpeg_is_kept_exactly_and_cache_is_bounded(self):
        from collections import OrderedDict
        worker = Worker.__new__(Worker)
        worker.local_preview = object()
        worker.preview_cache = OrderedDict()
        worker.preview_lock = threading.Lock()
        worker.c = {'local_preview_cache_frames': 2}
        for i in range(3):
            worker.cache_preview_source({'source_frame_id': i}, b'exact-jpeg-'+bytes([i]))
        self.assertEqual(list(worker.preview_cache), [1,2])
        self.assertEqual(worker.preview_cache[2][1], b'exact-jpeg-\x02')

    def test_gzip_foreign_results_rejected_and_latest_queue_bounded(self):
        from types import SimpleNamespace
        from lab.result_codec import encode_json
        worker = Worker.__new__(Worker)
        worker.local_preview = object()
        worker.root = 'labs/abc20261102/v1'
        worker.c = {'project': 'abc20261102'}
        worker.active_run = 'test'
        worker.preview_results = queue.Queue(1)
        def send(frame, run='test'):
            obs = {'project': 'abc20261102', 'run_id':run, 'producer':'cloud',
                   'source_frame_id':frame, 'padding':'x'*3000}
            worker.receive(None,None,SimpleNamespace(topic=worker.root+'/runs/'+run+'/observations',
                                                     payload=encode_json(obs,compress=True)))
        send(1); send(2); send(3,'foreign')
        self.assertEqual(worker.preview_results.qsize(), 1)
        self.assertEqual(worker.preview_results.get_nowait()['source_frame_id'], 2)


if __name__ == '__main__':
    unittest.main()
