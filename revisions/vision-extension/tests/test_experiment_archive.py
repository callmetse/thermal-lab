import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402

# begin() 要读 config.json：本机没有部署机的配置，用最小合法配置顶上，测试才能自带环境。
TEST_CONFIG = {"project": "abc20261102", "topic_root": "labs/abc20261102/v1",
               "mqtt_host": "127.0.0.1", "mqtt_port": 18830, "llm_model": "qwen2.5:7b",
               "sample_fps": 5, "capture_fps": 15, "max_people": 0, "pose_max_det": 4,
               "min_person_confidence": 0.2, "min_person_height_ratio": 0.1,
               "replay_file": "data/replay.mp4", "width": 1280, "height": 720}


class ArchiveTests(unittest.TestCase):
    def test_dedup_export_hashes_sensor_separation_and_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'source.db'
            (root / 'config.json').write_text(json.dumps(TEST_CONFIG), encoding='utf-8')
            old_config = os.environ.get('ABC_CONFIG')
            os.environ['ABC_CONFIG'] = str(root / 'config.json')
            try:
                self.run_case(root, source)
            finally:
                if old_config is None:
                    os.environ.pop('ABC_CONFIG', None)
                else:
                    os.environ['ABC_CONFIG'] = old_config

    def run_case(self, root, source):
        with closing(sqlite3.connect(source)) as db:
            db.execute('CREATE TABLE measurements(received_at_ms INTEGER)')
        store = CollectionBatches(root / 'archive.db', source, clock=lambda: 1000)
        store.start({'run_id': 'test', 'source': 'camera'})
        archive = ExperimentArchive(store, root)
        try:
            archive.begin({'run_id': 'test', 'source': 'camera'})
            payload = {'run_id': 'test', 'producer_instance_id': 'p', 'observation_seq': 1,
                       'tracks': [{'bbox': [10.0, 20.0, 30.0, 40.0], 'confidence': 0.5}],
                       'crossing_events': [{}]}
            archive.submit('observation', payload)
            archive.submit('observation', payload)
            archive.pending.join()
            with self.assertRaises(ValueError):
                archive.export('test')
            with closing(store.connect()) as db:
                for sensor, value in [('sht4x', 20), ('scd41', 30)]:
                    raw = {'sensor_id': sensor, 'quality': 'ok', 'values': {'temperature_c': value}}
                    db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,raw_json) '
                               'VALUES(?,?,?,?)', ('test', sensor, sensor, json.dumps(raw)))
                db.execute("UPDATE batches SET status='completed',ended_ms=2000 WHERE batch_id='test'")
                db.commit()
            summary = archive.summary('test')
            self.assertEqual(summary['observation_count'], 1)
            self.assertEqual(summary['environment_statistics']['sht4x/temperature_c']['mean'], 20)
            self.assertEqual(summary['environment_statistics']['scd41/temperature_c']['mean'], 30)
            exported = archive.export('test')
            try:
                with zipfile.ZipFile(exported) as z:
                    manifest = json.loads(z.read('checksums.json'))
                    for name, digest in manifest.items():
                        self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
                    self.assertEqual(len(z.read('observations.jsonl').splitlines()), 1)
                    self.assertFalse(json.loads(z.read('assets_manifest.json'))['media_included'])
            finally:
                shutil.rmtree(exported.parent)
            store.start({'run_id': 'old'})
            self.assertTrue(archive.summary('old')['historical_missing'])
        finally:
            archive.close()


if __name__ == '__main__':
    unittest.main()
