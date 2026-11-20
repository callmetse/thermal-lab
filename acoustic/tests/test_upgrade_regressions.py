"""Regression checks: full SSE ring buffers and real-time paced file replay."""
import asyncio
import collections
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class PushRegression(unittest.IsolatedAsyncioTestCase):
    async def test_new_event_pushed_when_ring_size_stays_120(self):
        import edge
        e=edge.Edge.__new__(edge.Edge)
        ring=collections.deque(({'event_id':str(i)} for i in range(120)),maxlen=120)
        e.snapshot=lambda:dict(session_id='s',status='recording',active='s',events=list(ring),confirmed_count=0,error=None)
        response=AsyncMock()
        rounds=0
        async def sleep(_):
            nonlocal rounds
            rounds+=1
            if rounds==1:ring.appendleft({'event_id':'new'})
            else:raise asyncio.CancelledError()
        with patch.object(edge.web,'StreamResponse',return_value=response),patch.object(edge.asyncio,'sleep',side_effect=sleep):
            await e.stream(None)
        frames=[json.loads(c.args[0].decode()[6:].strip()) for c in response.write.call_args_list]
        self.assertEqual(len(frames),2)
        self.assertEqual(len(frames[1]['events']),120)
        self.assertEqual(frames[1]['events'][0]['event_id'],'new')

class ReplayRegression(unittest.TestCase):
    def test_replay_is_paced_for_every_block(self):
        spec=importlib.util.spec_from_file_location('replay_audio',ROOT/'tools/replay_audio.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        virtual=[0.0];sent=[]
        class Client:
            def loop_stop(self):pass
        def publish(client,channel,payload,**kw):
            if channel=='audio':sent.append(virtual[0])
            return True
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'sample.wav'
            with wave.open(str(p),'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0'*(32000*3))
            with patch.object(module,'mqtt',return_value=Client()),patch.object(module,'publish',side_effect=publish),patch.object(module.time,'sleep',side_effect=lambda seconds:virtual.__setitem__(0,virtual[0]+seconds)),patch.object(module.time,'monotonic',side_effect=lambda:virtual[0]),patch.object(module.time,'monotonic_ns',side_effect=lambda:int(virtual[0]*1e9)),patch.object(sys,'argv',['replay_audio',str(p),'--session','test','--tail','0']),patch('sys.stdout',new_callable=io.StringIO):
                module.main()
        self.assertEqual(len(sent),12)
        self.assertAlmostEqual(sent[-1]-sent[0],2.75)
        self.assertTrue(all(abs(b-a-.25)<1e-7 for a,b in zip(sent,sent[1:])))
if __name__=='__main__':unittest.main()
