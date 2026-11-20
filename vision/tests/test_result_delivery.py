import json
from pathlib import Path
import queue
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab import llm
from lab.common import latest_put
from lab.result_codec import decode_json, encode_json


def result(frame=20, sequence=2, producer="boot-one", run_id="delivery_run"):
    value = {"project":"abc20261102", "run_id":run_id, "arm":"C", "producer":"cloud",
             "producer_instance_id":producer, "observation_seq":sequence,
             "source_frame_id":frame, "tracks":[], "clo":None, "met":None,
             "audit":"原始证据 " * 300}
    return value


class ObservationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.cursor = llm.ObservationCursor()
        self.cursor.start("delivery_run")

    def test_late_old_qos1_and_duplicate_do_not_replace_newest_queue(self):
        pending = queue.Queue(1)
        newest = result()
        for value in [newest, result(frame=10, sequence=1), newest]:
            if self.cursor.accept(value):
                latest_put(pending, value)
        self.assertEqual(pending.get_nowait(), newest)
        self.assertEqual(self.cursor.position, 2)

    def test_restart_accepts_reset_then_retires_every_previous_producer(self):
        self.assertTrue(self.cursor.accept(result()))
        restarted = result(frame=0, sequence=1, producer="boot-two")
        self.assertTrue(self.cursor.accept(restarted))
        self.assertFalse(self.cursor.current(result()))
        self.assertFalse(self.cursor.accept(result(frame=300, sequence=300)))
        self.assertTrue(self.cursor.accept(result(frame=0, sequence=1, producer="boot-three")))
        self.assertFalse(self.cursor.accept(result(frame=500, sequence=500, producer="boot-two")))

    def test_same_run_lease_keeps_cursor_but_new_run_clears_it(self):
        self.assertTrue(self.cursor.accept(result()))
        self.cursor.start("delivery_run")
        self.assertFalse(self.cursor.accept(result(frame=0, sequence=1)))
        self.cursor.start("next_run")
        self.assertTrue(self.cursor.accept(result(frame=0, sequence=1, run_id="next_run")))
        self.assertFalse(self.cursor.accept(result(frame=50, sequence=50)))

    def test_legacy_uses_producer_and_frame_without_removing_empty_evidence(self):
        value = result()
        for name in ["producer_instance_id", "observation_seq"]:
            value.pop(name)
        before = json.loads(json.dumps(value))
        self.assertTrue(self.cursor.accept(value))
        self.assertFalse(self.cursor.accept(value))
        self.assertFalse(self.cursor.accept({**value, "source_frame_id":19}))
        self.assertTrue(self.cursor.accept({**value, "source_frame_id":21}))
        self.assertEqual(value, before)
        self.assertEqual(value["tracks"], [])
        self.assertIsNone(value["clo"])
        self.assertIsNone(value["met"])

    def test_plain_and_gzip_cannot_bypass_duplicate_filter(self):
        value = result()
        self.assertTrue(self.cursor.accept(decode_json(encode_json(value))))
        self.assertFalse(self.cursor.accept(decode_json(encode_json(value, compress=True))))
        self.assertFalse(self.cursor.accept({**value, "observation_seq":None}))

    def test_real_receive_uses_cursor_and_generation_traces_original_observation(self):
        c = {"project":"abc20261102", "topic_root":"labs/abc20261102/v1",
             "llm_interval_s":3, "llm_model":"qwen2.5:7b", "llm_url":"http://fixture",
             "llm_max_tokens":128}
        client = Mock()
        event = Mock()
        event.is_set.side_effect = [False, True]
        newest = result()
        command = {"project":c["project"], "op":"start", "run_id":"delivery_run",
                   "arm":"C", "source":"replay", "seconds":60, "issued_ms":time.time()*1000}
        def connect(config, role, receive):
            receive(client, None, SimpleNamespace(topic=c["topic_root"]+"/commands",
                    retain=False, payload=json.dumps(command).encode()))
            for value in [newest, result(frame=10, sequence=1), newest]:
                receive(client, None, SimpleNamespace(topic=c["topic_root"]+"/runs/delivery_run/observations",
                        payload=encode_json(value, compress=True)))
            return client
        reply = {"response":json.dumps({"summary":"未检出人物", "uncertainty":"待确认"})}
        with patch.object(llm, "config", return_value=c), \
             patch.object(llm.threading, "Event", return_value=event), \
             patch.object(llm, "mqtt_client", side_effect=connect), \
             patch.object(llm.signal, "signal"), \
             patch.object(llm, "request_json", side_effect=[{"models":[{"name":"qwen2.5:7b"}]},reply]) as request, \
             patch.object(llm, "publish") as publish:
            llm.main()
        generated = publish.call_args.args[2]
        self.assertEqual(generated["source_frame_id"],20)
        self.assertEqual(generated["producer_instance_id"],"boot-one")
        self.assertEqual(generated["observation_seq"],2)
        payload = request.call_args.args[1]
        self.assertEqual(payload["system"],llm.SYSTEM)
        self.assertEqual(payload["prompt"],json.dumps(llm.facts(newest),ensure_ascii=False))
        self.assertEqual(payload["model"],"qwen2.5:7b")


if __name__ == "__main__":
    unittest.main()
