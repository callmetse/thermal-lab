import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.sample_store import BoundedSampleStore


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def meta(**overrides):
    base = {'task': 'person_detection', 'run_id': 'run-a',
            'trigger_reasons': ['random_audit'], 'sampling_probability': 0.02}
    base.update(overrides)
    return base


def frame(n):
    return bytes([n % 256]) * (100 + n)


class StoreTests(unittest.TestCase):
    def store(self, directory, **overrides):
        kwargs = {'max_bytes': 10 ** 6, 'max_samples': 100, 'max_age_s': 3600}
        kwargs.update(overrides)
        return BoundedSampleStore(directory, clock=kwargs.pop('clock', FakeClock()), **kwargs)

    def test_put_writes_image_and_sidecar_without_tmp_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.store(d)
            result = s.put(frame(1), meta())
            self.assertEqual(result['status'], 'saved')
            files = sorted(p.name for p in (Path(d) / 'samples').iterdir())
            self.assertEqual(len(files), 2)
            self.assertFalse(any(name.endswith('.tmp') for name in files))
            record = json.loads((Path(d) / 'samples' / f"{result['sample_id']}.json").read_text())
            self.assertEqual(record['schema_version'], 'hard_example_v1')
            self.assertEqual(record['label_status'], 'unlabeled')
            self.assertIn('media_sha256', record)

    def test_identical_frame_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.store(d)
            self.assertEqual(s.put(frame(1), meta())['status'], 'saved')
            self.assertEqual(s.put(frame(1), meta())['status'], 'duplicate')
            self.assertEqual(s.stats()['samples'], 1)
            self.assertEqual(s.stats()['counters']['duplicate'], 1)

    def test_max_samples_evicts_oldest(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock()
            s = self.store(d, max_samples=3, clock=clock)
            for i in range(5):
                s.put(frame(i), meta())
                clock.advance(1)
            entries = s.scan()
            self.assertEqual(len(entries), 3)
            self.assertEqual(len({e['sample_id'] for e in entries}), 3)
            self.assertEqual(s.stats()['counters']['evicted'], 2)

    def test_max_bytes_evicts_until_under_limit(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock()
            s = self.store(d, max_bytes=250, max_samples=100, clock=clock)
            for i in range(4):                       # 每帧 100,101,102,103 字节
                s.put(frame(i), meta())
                clock.advance(1)
            self.assertLessEqual(s.stats()['bytes'], 250)
            self.assertGreaterEqual(s.stats()['samples'], 1)

    def test_age_eviction(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock()
            s = self.store(d, max_age_s=10, clock=clock)
            s.put(frame(1), meta())
            clock.advance(20)
            s.put(frame(2), meta())
            self.assertEqual(s.stats()['samples'], 1)

    def test_labelled_samples_are_never_evicted(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock()
            s = self.store(d, max_samples=1, clock=clock)
            s.put(frame(1), meta(label_status='human_verified', locked=True))
            clock.advance(1)
            s.put(frame(2), meta())
            entries = s.scan()
            labels = sorted(e['label_status'] for e in entries)
            self.assertIn('human_verified', labels)

    def test_corrupt_sidecar_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.store(d)
            s.put(frame(1), meta())
            bad = Path(d) / 'samples' / 'bad.json'
            bad.write_text('{not json', encoding='utf-8')
            self.assertEqual(len(s.scan()), 1)

    def test_stats_report_reason_counts(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.store(d)
            s.put(frame(1), meta(trigger_reasons=['random_audit']))
            s.put(frame(2), meta(trigger_reasons=['motion_without_person']))
            stats = s.stats()
            self.assertEqual(stats['samples'], 2)
            self.assertEqual(stats['by_reason'], {'random_audit': 1, 'motion_without_person': 1})
            self.assertEqual(stats['unlabeled'], 2)


if __name__ == '__main__':
    unittest.main()
