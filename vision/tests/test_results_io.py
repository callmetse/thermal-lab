import csv
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.result_codec import decode_json, encode_json
sys.path.insert(0, str(ROOT / "tools"))
from export_results import export_records, main
from lab.worker import Worker


class ResultCodecTests(unittest.TestCase):
    def test_plain_and_gzip_keep_null_unicode_numbers_and_nested_evidence(self):
        value = {"run_id": "本轮", "source_frame_id": 9007199254740993,
                 "source_pts_s": 1.23456789012345, "clo": None, "met": None,
                 "tracks": [{"track_id": i, "clothing": [{"category": "上衣", "confidence": .87654321012345}],
                              "action": {"value": "unknown", "reason": "insufficient_sampling"}} for i in range(8)]}
        plain = encode_json(value)
        zipped = encode_json(value, compress=True)
        self.assertTrue(zipped.startswith(b"\x1f\x8b"))
        self.assertLess(len(zipped), len(plain))
        self.assertEqual(gzip.decompress(zipped), plain)
        self.assertEqual(decode_json(plain), value)
        self.assertEqual(decode_json(zipped), value)
        self.assertEqual(decode_json(plain.decode("utf-8")), value)

    def test_invalid_truncated_and_oversized_results_are_rejected(self):
        zipped = gzip.compress(b'{"large":"' + b"x" * 4096 + b'"}')
        with self.assertRaises(ValueError):decode_json(zipped, max_uncompressed_bytes=128)
        with self.assertRaises(ValueError):decode_json(b"x" * 129, max_uncompressed_bytes=128)
        with self.assertRaises(ValueError):decode_json(zipped[:-5])
        with self.assertRaises(ValueError):decode_json(b"not json")


class HistoryExportTests(unittest.TestCase):
    def test_filters_full_records_and_keeps_empty_observation_and_null_csv(self):
        empty = {"channel": "observations", "console_boot_id": "boot", "received_mono_ms": 12.5,
                 "payload": {"run_id": "r", "source_frame_id": 1, "tracks": [], "extra_evidence": {"keep": True}}}
        full = {"channel": "observations", "payload": {"run_id": "r", "source_frame_id": 2, "tracks": [
            {"track_id": 1, "clothing": [{"category": "上衣", "confidence": .7}], "clo": None, "met": None},
            {"track_id": 2, "clothing": [], "clo": .5, "met": 1.2}]}}
        llm = {"channel": "llm", "payload": {"run_id": "r", "draft": {"summary": "未确认"}}}
        other = {"channel": "observations", "payload": {"run_id": "other", "tracks": []}}
        original = "\n".join(json.dumps(r, ensure_ascii=False) for r in [empty, full, llm, other])
        output, people = io.StringIO(), io.StringIO()
        counts = export_records(io.StringIO(original), output, run_ids=["r"], channels=["observations"], people_csv=people)
        self.assertEqual([json.loads(s) for s in output.getvalue().splitlines()], [empty, full])
        rows = list(csv.DictReader(io.StringIO(people.getvalue())))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["row_type"], "empty_observation")
        self.assertEqual(rows[0]["track_count"], "0")
        self.assertEqual(rows[1]["clo"], "")
        self.assertEqual(rows[1]["met"], "")
        self.assertEqual(rows[2]["clo"], "0.5")
        self.assertEqual(json.loads(rows[1]["clothing_json"])[0]["category"], "上衣")
        self.assertEqual(counts, {"scanned_records":4,"exported_records":2,"csv_rows":3})

    def test_default_jsonl_export_keeps_non_observation_channels(self):
        rows = [{"channel": "llm", "payload": {"run_id": "r"}}, {"channel": "events", "payload": {"run_id": "r"}}]
        raw = "\n".join(json.dumps(r) for r in rows)
        output = io.StringIO()
        export_records(io.StringIO(raw), output)
        self.assertEqual([json.loads(s) for s in output.getvalue().splitlines()], rows)

    def test_cli_rejects_input_output_collision_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            original = '{"channel":"events","payload":{"run_id":"r"}}\n'
            path.write_text(original, encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--input", str(path), "--output", str(path)])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_malformed_history_is_reported_with_line_number(self):
        with self.assertRaisesRegex(ValueError, "line 2"):
            export_records(io.StringIO('{"channel":"events","payload":{}}\ninvalid\n'), io.StringIO())


class ProducerJournalTests(unittest.TestCase):
    def worker(self, journal=True, qos=0):
        worker = Worker.__new__(Worker)
        worker.local_preview = None
        worker.c = {"result_journal": journal, "result_qos": qos}
        worker.role, worker.root = "edge", "labs/test/v1"
        worker.client, worker.producer_instance_id = object(), "instance-a"
        worker.active_run = "run-a"
        worker._result_journal, worker._result_stats = None, None
        worker.event = Mock()
        return worker

    def test_disconnected_publish_keeps_every_generated_result_and_closes_file(self):
        worker = self.worker()
        run = {"run_id": "run-a", "arm": "A"}
        observed = []
        opened = []
        def edge(current, cancel):
            for frame in [8, 11]:
                result = {"run_id": current["run_id"], "source_frame_id": frame,
                          "tracks": [{"track_id": 1, "clo": None, "met": None}]}
                observed.append(result)
                self.assertEqual(worker.publish_observation(current, result), 0)
            opened.append(worker._result_journal)
        worker.edge = edge
        with tempfile.TemporaryDirectory() as directory, patch("lab.worker.ROOT", Path(directory)), patch("lab.worker.publish", return_value=0):
            worker.session(run, None)
            path = Path(directory) / "data/results/run-a/edge-instance-a.jsonl"
            raw = path.read_bytes()
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual([r["payload"] for r in records], observed)
            self.assertEqual([r["payload"]["observation_seq"] for r in records], [1, 2])
            self.assertTrue(all(r["channel"] == "observations" for r in records))
            self.assertTrue(all(r["payload"]["tracks"][0]["clo"] is None for r in records))
            self.assertTrue(opened[0].closed)
            self.assertIsNone(worker._result_journal)
            stats = next(call.kwargs for call in worker.event.call_args_list if call.args[1] == "result_stats")
            self.assertEqual(stats["generated"], 2)
            self.assertEqual(stats["accepted_for_publish"], 0)
            self.assertEqual(stats["publish_rejected"], 2)
            self.assertEqual(stats["raw_journal_bytes"], len(raw))
            output = io.StringIO()
            csv_output = io.StringIO()
            export_records(io.StringIO(raw.decode()), output, run_ids=["run-a"], people_csv=csv_output)
            self.assertEqual([json.loads(s) for s in output.getvalue().splitlines()], records)
            rows = list(csv.DictReader(io.StringIO(csv_output.getvalue())))
            self.assertEqual([row["observation_seq"] for row in rows], ["1", "2"])
            self.assertTrue(all(row["producer_instance_id"] == "instance-a" for row in rows))
            self.assertTrue(all(row["producer_recorded_wall_ms"] for row in rows))

    def test_default_journal_off_and_sequence_resets_each_session(self):
        worker = self.worker(journal=False, qos=1)
        sequences = []
        def edge(current, cancel):
            value = {"run_id": current["run_id"], "tracks": []}
            worker.publish_observation(current, value)
            sequences.append(value["observation_seq"])
        worker.edge = edge
        with tempfile.TemporaryDirectory() as directory, patch("lab.worker.ROOT", Path(directory)), patch("lab.worker.publish", return_value=123) as publish:
            for run_id in ["run-a", "run-b"]:worker.session({"run_id": run_id, "arm": "A"}, None)
            self.assertFalse((Path(directory) / "data/results").exists())
            self.assertEqual(sequences, [1, 1])
            self.assertTrue(all(call.kwargs["qos"] == 1 for call in publish.call_args_list))
            self.assertEqual(worker._result_stats["accepted_for_publish"], 1)
            self.assertEqual(worker._result_stats["raw_journal_bytes"], 0)

    def test_disk_failure_stops_session_and_reports_failed_without_network_publish(self):
        worker = self.worker()
        reached_after_write = []
        def edge(current, cancel):
            worker.publish_observation(current, {"run_id": current["run_id"], "tracks": []})
            reached_after_write.append(True)
        worker.edge = edge
        with tempfile.TemporaryDirectory() as directory, patch("lab.worker.ROOT", Path(directory)), patch("pathlib.Path.open", side_effect=OSError("disk full")), patch("lab.worker.publish") as publish:
            worker.session({"run_id": "run-a", "arm": "A"}, None)
            self.assertFalse(reached_after_write)
            publish.assert_not_called()
            failed = [call for call in worker.event.call_args_list if call.args[1] == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("disk full", failed[0].kwargs["error"])

    def test_new_worker_instances_have_distinct_producer_ids(self):
        with patch("lab.worker.config", return_value={"topic_root": "labs/test/v1", "mqtt_host":"127.0.0.1", "mqtt_port":28883}), patch("lab.worker.mqtt_client", return_value=Mock()):
            first, second = Worker("edge"), Worker("edge")
        self.assertNotEqual(first.producer_instance_id, second.producer_instance_id)

    def test_flush_or_close_failure_reports_failed_and_still_finishes_session(self):
        for failing_operation in ["flush", "close"]:
            with self.subTest(operation=failing_operation), tempfile.TemporaryDirectory() as directory:
                worker = self.worker()
                journal = Mock()
                getattr(journal, failing_operation).side_effect = OSError(failing_operation + " failed")
                worker.edge = lambda run, cancel: worker.publish_observation(run, {"run_id": run["run_id"], "tracks": []})
                with patch("lab.worker.ROOT", Path(directory)), patch("pathlib.Path.open", return_value=journal), patch("lab.worker.publish", return_value=1):
                    worker.session({"run_id": "run-a", "arm": "A"}, None)
                failed = [call for call in worker.event.call_args_list if call.args[1] == "failed"]
                self.assertEqual(len(failed), 1)
                self.assertIn(failing_operation + " failed", failed[0].kwargs["error"])
                kinds = [call.args[1] for call in worker.event.call_args_list]
                self.assertIn("result_stats", kinds)
                self.assertEqual(kinds[-1], "role_finished")
                self.assertIsNone(worker._result_journal)
                self.assertIsNone(worker.active_run)
                journal.close.assert_called_once()

    def test_edge_b_c_do_not_create_result_journal_or_change_uploaded_jpeg(self):
        for arm in ["B", "C"]:
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as directory:
                worker = self.worker()
                # Exercise the actual edge routing with a single capture fixture.
                worker.c.update({"project":"test","allow_camera":False,"replay_file":"data/replay.mp4","sample_fps":20,
                                 "encode_max_side":640,"jpeg_quality":55,"gate_imgsz":416,"preview_hz":12})
                worker.stop = threading.Event()
                worker.gate = Mock()
                worker.gate.predict.return_value = [SimpleNamespace(boxes=[object()])]
                client = object()
                worker.media = lambda: client
                image = SimpleNamespace(shape=(480, 640, 3))
                jpeg = b"unchanged-jpeg-input"
                cap = Mock()
                cap.isOpened.return_value, cap.get.return_value = True, 30
                cap.read.side_effect = [(True, image), (False, None)]
                cv2 = SimpleNamespace(VideoCapture=lambda *_:cap, CAP_PROP_FPS=5, IMWRITE_JPEG_QUALITY=1,
                                      IMREAD_COLOR=1, imencode=Mock(return_value=(True, SimpleNamespace(tobytes=lambda:jpeg))),
                                      imdecode=Mock(return_value=image))
                replay = Path(directory) / "data/replay.mp4"
                replay.parent.mkdir();replay.write_bytes(b"capture fixture")
                # Restore the real method; other tests replace it with session fixtures.
                worker.edge = Worker.edge.__get__(worker, Worker)
                with patch("lab.worker.ROOT", Path(directory)), patch.dict(sys.modules, {"cv2":cv2}), patch("lab.worker.publish", return_value=1) as publish:
                    worker.session({"run_id":"run-a","arm":arm,"source":"replay","seconds":1}, threading.Event())
                frames = [call for call in publish.call_args_list if call.args[1].endswith("/frames")]
                self.assertEqual(len(frames), 1)
                from lab.common import unpack
                header, uploaded = unpack(frames[0].args[2], worker.c.get("project", "test"))
                self.assertEqual(uploaded, jpeg)
                self.assertEqual(header["source_frame_id"], 0)
                self.assertEqual(header["source_pts_s"], 0)
                self.assertEqual(worker._result_stats["generated"], 0)
                self.assertFalse((Path(directory) / "data/results").exists())


if __name__ == "__main__":
    unittest.main()
