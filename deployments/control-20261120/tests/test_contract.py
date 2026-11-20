import copy
import json
from pathlib import Path
import queue
import sys
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.common import accepted_command, config, latest_put, pack, unpack, publish
from lab.vision import Vision
sys.path.insert(0, str(ROOT / "tools"))
from report import summarize


class ContractTests(unittest.TestCase):
    def test_preview_grid_does_not_reduce_eight_hz_to_six_with_twelve_hz_input(self):
        from lab.presentation import next_preview_slot
        previous = -1
        selected = []
        for frame in range(120):
            slot = next_preview_slot(frame / 12, 8, previous)
            if slot is not None:
                selected.append(frame)
                previous = slot
        self.assertEqual(len(selected), 80)
        self.assertIsNone(next_preview_slot(100, 0, previous))

    def test_display_projection_preserves_model_result_without_mutating_evidence(self):
        from lab.presentation import compact_observation
        obs = {'run_id': 'r', 'source_frame_id': 4, 'encoded_size': [640, 480], 'jpeg_sha256': 'source',
               'tracks': [{'track_id': 2, 'bbox': [2, 3, 40, 90], 'posture': {'state': 'standing'},
                           'action': {'value': 'unknown', 'probabilities': {'other': .9}},
                           'clothing': [{'category': 'pants', 'confidence': .8, 'bbox': [2, 50, 40, 90]}],
                           'clo': None, 'met': None, 'visible_clo_reference': {'items': ['large debug table']}}]}
        before = copy.deepcopy(obs)
        compact = compact_observation(obs)
        self.assertEqual(compact['jpeg_sha256'], 'source')
        self.assertEqual(compact['encoded_size'], [640, 480])
        self.assertEqual(compact['tracks'][0]['bbox'], obs['tracks'][0]['bbox'])
        self.assertEqual(compact['tracks'][0]['clothing'][0]['category'], 'pants')
        self.assertNotIn('visible_clo_reference', compact['tracks'][0])
        self.assertIsNone(compact['tracks'][0]['met'])
        self.assertEqual(before, obs)

    def test_llm_can_report_counts_but_not_clothing_absence(self):
        from lab.llm import validate_reply
        self.assertEqual(validate_reply({'summary': '检测到8个轨迹，衣物未检出', 'uncertainty': '动作未知'}), (True, []))
        schema, flags = validate_reply({'summary': '6人均未穿着衣物', 'uncertainty': '未知'})
        self.assertTrue(schema)
        self.assertIn('unsupported_clothing_absence_claim', flags)
        self.assertFalse(validate_reply({'summary': 8, 'uncertainty': '未知'})[0])

    def test_multi_person_selection_keeps_small_and_more_than_three_tracks(self):
        sys.path.insert(0, str(ROOT / 'vendor/algorithm'))
        from person_selection import select_people
        boxes = [[i * 2, 0, i * 2 + 1, 8 + i] for i in range(12)]
        selected = select_people(boxes, [.8] * 12, 640)
        self.assertEqual(set(selected), set(range(12)))
        self.assertEqual(len(select_people(boxes, [.8] * 12, 640, max_people=5)), 5)
        self.assertEqual(select_people(boxes, [.1] * 12, 640), [])

    def setUp(self):
        self.c = config()
        self.header = {"project": self.c["project"], "run_id": "trial_1", "source_frame_id": 4}
        self.cmd = {"project": self.c["project"], "run_id": "trial_1", "op": "start", "arm": "C",
                    "source": "replay", "seconds": 30, "issued_ms": time.time() * 1000}

    def test_binary_roundtrip_and_foreign_project_rejection(self):
        blob = pack(self.header, b"JPEG bytes")
        h, jpg = unpack(blob, self.c["project"])
        self.assertEqual(h, self.header)
        self.assertEqual(jpg, b"JPEG bytes")
        with self.assertRaises(ValueError):
            unpack(blob, "other_project")

    def test_malformed_envelopes(self):
        for blob in (b"", b"\xff\xff\xff\xffabc", b"\x00\x00\x00\x10{}"):
            with self.assertRaises(ValueError):
                unpack(blob, self.c["project"])

    def test_latest_frame_drops_old_pending_frame(self):
        q = queue.Queue(1)
        self.assertFalse(latest_put(q, "old"))
        self.assertTrue(latest_put(q, "new"))
        self.assertEqual(q.get_nowait(), "new")

    def test_qos0_backlog_is_bounded_without_starving_preview(self):
        messages = []
        info = SimpleNamespace(rc=0, is_published=lambda: False)
        client = SimpleNamespace(is_connected=lambda: True,
                                 publish=lambda *args, **kw: messages.append(args) or info)
        self.assertGreater(publish(client, "lab/observations", b"one"), 0)
        self.assertEqual(publish(client, "lab/observations", b"two"), 0)
        self.assertGreater(publish(client, "lab/llm", b"other"), 0)
        self.assertEqual(len(messages), 2)

    def test_remote_frame_confirmation_bounds_inflight_and_resumes_after_ack(self):
        from unittest.mock import patch
        from types import ModuleType
        properties = ModuleType('paho.mqtt.properties')
        properties.Properties = lambda *_: SimpleNamespace()
        packets = ModuleType('paho.mqtt.packettypes')
        packets.PacketTypes = SimpleNamespace(PUBLISH=3)
        messages, infos = [], []
        def send(topic, body, **kw):
            info = SimpleNamespace(rc=0, acked=False)
            info.is_published = lambda: info.acked
            messages.append((topic, body, kw));infos.append(info)
            return info
        client = SimpleNamespace(is_connected=lambda: True, publish=send, abc_frame_ack_window=4)
        with patch.dict(sys.modules, {'paho.mqtt.properties':properties,'paho.mqtt.packettypes':packets}):
            for i in range(4):self.assertGreater(publish(client,'lab/frames',str(i).encode()),0)
            self.assertEqual(publish(client,'lab/frames',b'drop-while-unconfirmed'),0)
            self.assertEqual(len(messages),4)
            self.assertTrue(all(m[2]['qos']==1 for m in messages))
            infos[0].acked=True
            self.assertGreater(publish(client,'lab/frames',b'fresh-after-ack'),0)
            self.assertEqual(len(client.abc_frame_pending),4)

    def test_command_namespace_expiry_and_id(self):
        self.assertTrue(accepted_command(self.cmd, self.c))
        for change in ({"project": "thermal"}, {"issued_ms": 0}, {"run_id": "../../old"}, {"arm": "D"}, {"seconds": 0}):
            self.assertFalse(accepted_command({**self.cmd, **change}, self.c))

    def test_low_visibility_never_implies_posture_or_met(self):
        person = {"tracking_id": 1, "bbox_xyxy": [0, 0, 100, 200], "keypoints_coco17": [[0, 0, 0]] * 17,
                  "temporal_window": {"ready": False}, "activity_candidate": {"value": "sit_down"},
                  "clothing": [], "visible_clothing_reference": {}}
        v = Vision.__new__(Vision)
        v.pipeline = SimpleNamespace(frame=lambda *_: ([copy.deepcopy(person)], None))
        tracks, _ = v.infer(None, 0)
        self.assertEqual(tracks[0]["posture"]["state"], "unknown")
        self.assertEqual(tracks[0]["action"]["value"], "unknown")
        self.assertIsNone(tracks[0]["met"])
        self.assertIsNone(tracks[0]["clo"])

    def test_reports_use_control_clock_and_count_abstentions(self):
        base = {"run_id": "r", "arm": "C"}
        rows = [
            {"channel": "commands", "received_mono_ms": 100, "payload": {**base, "op": "start"}},
            {"channel": "observations", "received_mono_ms": 150, "payload": {**base, "infer_ms": 10, "tracks": [{"action": {"value": "unknown"}, "clo": None, "met": None}]}},
            {"channel": "observations", "received_mono_ms": 250, "payload": {**base, "infer_ms": 20, "tracks": []}},
        ]
        report = summarize(rows)[0]
        self.assertEqual(report["start_to_first_observation_ms"], 50)
        self.assertEqual(report["unknown_action_fraction"], 1)
        self.assertEqual(report["clo_met_numerical_coverage"], 0)
        self.assertIsNone(report["capture_to_display_ms"])

    def test_flows_use_new_broker_and_new_log_path(self):
        flows = json.loads((ROOT / "deploy/nodered/flows.json").read_text())
        brokers = [n for n in flows if n["type"] == "mqtt-broker"]
        self.assertEqual(len(brokers), 1)
        self.assertEqual(brokers[0]["broker"], "broker")
        for n in flows:
            if n.get("z") == "lab":
                self.assertIsInstance(n.get("x"), (float, int))
                self.assertIsInstance(n.get("y"), (float, int))
            if n["type"] == "mqtt in":
                self.assertTrue(n["topic"].startswith(self.c["topic_root"] + "/"))
            if n["type"] == "file":
                self.assertEqual(n["filename"], "/data/lab/events.jsonl")

    def test_control_restart_never_combines_monotonic_clocks(self):
        rows = [
            {"channel": "commands", "console_boot_id": "one", "received_mono_ms": 9999,
             "payload": {"run_id": "r", "arm": "C", "op": "start"}},
            {"channel": "observations", "console_boot_id": "two", "received_mono_ms": 10,
             "payload": {"run_id": "r", "arm": "C", "infer_ms": 1, "tracks": []}},
        ]
        reports = summarize(rows)
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(r["start_to_first_observation_ms"] is None for r in reports))

    def test_llm_idle_does_not_access_model_or_uninitialized_result(self):
        from lab import llm
        event = Mock()
        event.is_set.side_effect = [False, True]
        empty = Mock()
        empty.get.side_effect = queue.Empty
        client = Mock()
        with patch.object(llm.threading, "Event", return_value=event), \
             patch.object(llm.queue, "Queue", return_value=empty), \
             patch.object(llm, "mqtt_client", return_value=client), \
             patch.object(llm.signal, "signal"), \
             patch.object(llm, "request_json") as request:
            llm.main()
            request.assert_not_called()

    def test_late_results_do_not_count_as_timely_predictions(self):
        rows = [
            {"channel": "commands", "received_mono_ms": 100, "payload": {"run_id": "r", "arm": "C", "op": "start"}},
            {"channel": "commands", "received_mono_ms": 200, "payload": {"run_id": "r", "arm": "C", "op": "stop"}},
            {"channel": "observations", "received_mono_ms": 300, "payload": {"run_id": "r", "arm": "C", "infer_ms": 10, "tracks": []}},
        ]
        r = summarize(rows)[0]
        self.assertEqual(r["observations"], 0)
        self.assertEqual(r["late_observations_after_stop"], 1)
        self.assertIsNone(r["start_to_first_observation_ms"])


if __name__ == "__main__":
    unittest.main()
