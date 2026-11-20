import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from collaboration_report import load, summarize


def record(run_id, duration=20.0, frame_bytes=7_000_000, mode='shadow', dropped=None,
           gate=None, health=None, fallback=0):
    return {
        'run_id': run_id, 'arm': 'C', 'duration_s': duration, 'frame_bytes': frame_bytes,
        'mode': mode, 'budget_bps': 4_000_000, 'target_fps': 14.5, 'sent_frames': 280,
        'dropped': dropped or {}, 'scene_gate': gate or {}, 'health': health or {},
        'preview_fallback_packets': fallback,
    }


class CollaborationReportTests(unittest.TestCase):
    def test_load_filters_by_date_and_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '2026-11-16.jsonl').write_text(
                json.dumps(record('run-a')) + '\n' + 'not json\n', encoding='utf-8')
            (root / '2026-11-15.jsonl').write_text(json.dumps(record('run-b')) + '\n', encoding='utf-8')
            (root / '2026-11-14.jsonl').write_text(json.dumps(record('run-c')) + '\n', encoding='utf-8')
            today = date(2026, 11, 16)
            self.assertEqual([r['run_id'] for r in load(root, 1, today)], ['run-a'])
            self.assertEqual(sorted(r['run_id'] for r in load(root, 2, today)), ['run-a', 'run-b'])
            self.assertEqual(len(load(root, 3, today)), 3)

    def test_summary_merges_drops_gate_and_recovery(self):
        records = [
            record('a', duration=20, frame_bytes=7_000_000, dropped={'pacing': 10},
                   gate={'allowed': 30, 'skipped': 5, 'by_state': {'empty_verified': 20}},
                   health={'state': 'READY', 'recovery_s': 4.5, 'transitions': [{}, {}]}, fallback=7),
            record('b', duration=10, frame_bytes=3_000_000, mode='active', dropped={'pacing': 5, 'byte_budget': 2},
                   gate={'allowed': 10, 'skipped': 15, 'by_state': {'empty_verified': 15}},
                   health={'state': 'READY', 'transitions': [{}]}, fallback=0),
        ]
        s = summarize(records)
        self.assertEqual(s['sessions'], 2)
        self.assertEqual(s['duration_s'], 30.0)
        self.assertAlmostEqual(s['media_mbps'], round(10_000_000 * 8 / 30 / 1e6, 3))
        self.assertEqual(s['drops'], {'pacing': 15, 'byte_budget': 2})
        self.assertEqual(s['scene_gate'], {'allowed': 40, 'skipped': 20, 'by_state': {'empty_verified': 35}})
        self.assertEqual(s['modes'], {'shadow': 1, 'active': 1})
        self.assertEqual(s['recoveries_s'], [4.5])
        self.assertEqual(s['health_transitions'], 3)
        self.assertEqual(s['fallback_packets'], 7)

    def test_empty_input_does_not_divide_by_zero(self):
        s = summarize([])
        self.assertEqual(s['sessions'], 0)
        self.assertIsNone(s['media_mbps'])


if __name__ == '__main__':
    unittest.main()
