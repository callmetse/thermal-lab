import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

if (ROOT / 'sample_store.py').is_file():
    from sample_store import BoundedSampleStore
else:
    from lab.sample_store import BoundedSampleStore
from export_hard_examples import main as export_main


def frame(n):
    return bytes([n]) * (120 + n)


class ExportTests(unittest.TestCase):
    def fill(self, directory, count=2):
        store = BoundedSampleStore(directory)
        for i in range(count):
            store.put(frame(i), {'task': 'person_detection', 'run_id': 'r',
                                 'trigger_reasons': ['random_audit'],
                                 'sampling_probability': 0.02,
                                 'arm': 'C', 'gate_state': 'active', 'health_state': 'READY'})
        return store

    def test_export_writes_self_contained_package(self):
        with tempfile.TemporaryDirectory() as d:
            store_dir = Path(d) / 'store'
            out = Path(d) / 'out'
            self.fill(store_dir, 2)
            code = export_main(['--store', str(store_dir), '--out', str(out)])
            self.assertEqual(code, 0)
            manifest = [json.loads(line) for line in
                        (out / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(manifest), 2)
            self.assertTrue(all(row['verified'] for row in manifest))
            self.assertEqual(len(list((out / 'samples').glob('*.jpg'))), 2)
            header = (out / 'labels_template.csv').read_text(encoding='utf-8').splitlines()
            self.assertIn('source_sha256', header[0])
            self.assertEqual(len(header), 3)
            summary = json.loads((out / 'dataset_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['samples'], 2)
            self.assertEqual(summary['corrupted'], [])

    def test_corrupted_image_is_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            store_dir = Path(d) / 'store'
            out = Path(d) / 'out'
            store = self.fill(store_dir, 2)
            victim = next((store_dir / 'samples').glob('*.jpg'))
            victim.write_bytes(b'tampered')
            code = export_main(['--store', str(store_dir), '--out', str(out)])
            self.assertEqual(code, 3)
            summary = json.loads((out / 'dataset_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(len(summary['corrupted']), 1)
            self.assertEqual(summary['samples'], 1)

    def test_empty_store_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(export_main(['--store', str(Path(d) / 'none'), '--out', str(Path(d) / 'o')]), 2)
            empty = Path(d) / 'empty'
            empty.mkdir()
            self.assertEqual(export_main(['--store', str(empty), '--out', str(Path(d) / 'o2')]), 1)


if __name__ == '__main__':
    unittest.main()
