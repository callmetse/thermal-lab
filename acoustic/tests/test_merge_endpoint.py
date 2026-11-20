"""HTTP merge handler: happy path plus the two rejection codes."""
import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import edge
    import merge
    EDGE_AVAILABLE = True
except Exception:                      # aiohttp only on the Pi host
    EDGE_AVAILABLE = False

from aiohttp import web


def transcript(eid, start_s, dur, text, session='s1'):
    return dict(event_id=eid, kind='transcript', session_id=session,
                speech_start_sample=int(start_s * 16000), speech_seconds=dur,
                text=text, candidate={'sensation': None, 'preference': None},
                source={'session_id': session, 'seq': 1},
                recorded_wall_ms=1700000000000)


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@unittest.skipUnless(EDGE_AVAILABLE, 'edge needs aiohttp (Pi host only)')
class MergeEndpoint(unittest.IsolatedAsyncioTestCase):
    def make_edge(self, events):
        e = edge.Edge.__new__(edge.Edge)
        e.lock = threading.Lock()
        e.events = __import__('collections').deque(events, maxlen=120)
        e.client = object()          # publish is patched in the tests
        return e

    async def test_merge_appends_event_and_journals(self):
        first = transcript('a', 1.0, 1.0, '我觉得')
        second = transcript('b', 2.1, 0.9, '现在有点热')
        e = self.make_edge([second, first])
        written = []
        with patch.object(edge, 'journal', side_effect=lambda name, item: written.append(item)), \
             patch.object(edge, 'publish', return_value=True):
            response = await e.merge_fragments(FakeRequest({'event_ids': ['a', 'b']}))
        payload = __import__('json').loads(response.text)
        self.assertEqual(payload['text'], '我觉得现在有点热')
        self.assertEqual(payload['source_event_ids'], ['a', 'b'])
        self.assertEqual(e.events[0]['event_id'], payload['event_id'])
        self.assertEqual(len(written), 1)
        self.assertEqual(len([x for x in e.events if x['kind'] == 'transcript']), 2)  # originals kept

    async def test_rejects_one_id(self):
        e = self.make_edge([transcript('a', 1.0, 1.0, '甲')])
        with self.assertRaises(web.HTTPBadRequest):
            await e.merge_fragments(FakeRequest({'event_ids': ['a']}))

    async def test_rejects_missing_event(self):
        e = self.make_edge([transcript('a', 1.0, 1.0, '甲')])
        with self.assertRaises(web.HTTPConflict):
            await e.merge_fragments(FakeRequest({'event_ids': ['a', 'zzz']}))


if __name__ == '__main__':
    unittest.main()
