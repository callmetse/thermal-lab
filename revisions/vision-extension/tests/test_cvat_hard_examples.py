import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from xml.etree import ElementTree as ET

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.cvat_hard_examples import dump, join_reviews, load, make_package, sha256
from lab.worker import Worker


class CVATTests(unittest.TestCase):
    def test_saved_b_sample_retains_capture_and_gate_provenance(self):
        worker = object.__new__(Worker)
        worker.c = {}
        worker.gate_model_sha256 = "a" * 64
        worker.hard_selector = Mock()
        worker.hard_selector.consider.return_value = {
            "capture": True, "reasons": ["random_audit"], "sampling_probability": 0.02,
        }
        worker.sample_store = Mock()
        worker.sample_store.put.return_value = {"status": "saved"}
        stats = {"hard_examples_saved": 0, "hard_examples_rejected": 0}
        clip_hash = "b" * 64
        for source, source_hash, gate_state, group in (
            ("camera", None, "negative", "run-1"),
            ("replay", clip_hash, "positive", clip_hash),
        ):
            with self.subTest(source=source):
                worker._maybe_save_hard_example(
                    {"run_id": "run-1", "source": source}, {"source_sha256": source_hash},
                    b"sample", {"state": "active"}, "READY", "B", stats, gate_state=gate_state,
                )
                metadata = worker.sample_store.put.call_args.args[1]
                self.assertEqual(metadata["student_model_version"], "a" * 64)
                self.assertEqual(metadata["capture_group"], group)
                self.assertEqual(metadata["source_sha256"], source_hash)
                self.assertEqual(metadata["gate_state"], gate_state)
                self.assertEqual(metadata["scene_gate_state"], "active")

    def test_review_round_trip_and_anti_leakage_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "source"
            images = outbox / "samples"
            images.mkdir(parents=True)
            rows = []
            for n in range(3):
                path = images / "incoming.jpg"
                Image.new("RGB", (200, 100), (n * 30, 3, 50)).save(path)
                digest = sha256(path)
                key = digest[:16]
                path.rename(images / (key + ".jpg"))
                rows.append({"sample_id": key, "media_sha256": digest, "verified_sha256": digest,
                             "verified": True, "media_bytes": (images / (key + ".jpg")).stat().st_size,
                             "encoded_size": [200, 100], "source": "camera", "run_id": "run-1" if n < 2 else "run-2",
                             "arm": "B"})
            (outbox / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            dump(outbox / "dataset_manifest.json", {"samples": 3, "corrupted": []})
            first, second, third = (row["sample_id"] for row in rows)
            teacher = {"model": "test-proposal-only", "sha256": "a" * 64,
                       "samples": {first: {"media_sha256": rows[0]["media_sha256"],
                                           "boxes": [{"xyxy": [10, 20, 80, 90], "confidence": 0.8}]}}}
            package = root / "task"
            self.assertEqual(make_package(outbox, package, "B", teacher)["with_teacher_proposals"], 1)
            self.assertEqual(load(package / "sample_map.json")["teacher"], teacher)
            xml = ET.parse(package / "teacher.xml")
            ET.SubElement(xml.find("meta/task"), "id").text = "100"
            box = xml.find("image/box")
            box.set("xtl", "20")
            box.set("source", "manual")
            corrected = root / "corrected.xml"
            xml.write(corrected)
            review = load(package / "review_template.json")
            review.update(task_id="100", reviewer="test fixture", annotation_revision="1")
            review["samples"][first].update(decision="positive", split="train", capture_group="site-session-1")
            review["samples"][second].update(decision="confirmed_empty", split="train", capture_group="site-session-1")
            review["samples"][third].update(decision="ambiguous")
            review_path = root / "review.json"
            dump(review_path, review)
            joined = join_reviews(package, corrected, review_path)
            self.assertEqual(joined["split_counts"], {"train": 2, "excluded": 1})
            self.assertEqual(joined["records"][0]["boxes"][0]["xyxy_normalized"], [0.1, 0.2, 0.4, 0.9])
            self.assertEqual(joined["records"][1]["boxes"], [])
            review["samples"][second].update(split="test", capture_group="different-name")
            dump(review_path, review)
            with self.assertRaisesRegex(ValueError, "run group crosses splits"):
                join_reviews(package, corrected, review_path)
            (package / "images" / (first + ".jpg")).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "Image hash changed"):
                join_reviews(package, corrected, review_path)


if __name__ == "__main__":
    unittest.main()
