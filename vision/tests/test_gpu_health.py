import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.gpu_health import GpuHealth


class FakeClock:
    def __init__(self, start=500.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def health(**overrides):
    clock = FakeClock()
    config = {'offline_after_s': 6.0, 'warmup_timeout_s': 25.0,
              'degraded_queue_ms': 150.0, 'degraded_infer_ms': 120.0, 'degraded_streak': 3}
    config.update(overrides)
    return GpuHealth(config, clock=clock), clock


class GpuHealthTests(unittest.TestCase):
    def test_starts_offline_and_goes_warming_on_session(self):
        h, clock = health()
        self.assertEqual(h.state, 'OFFLINE')
        h.start_session()
        self.assertEqual(h.state, 'WARMING')
        self.assertFalse(h.ready)

    def test_first_observation_makes_it_ready_without_recovery_metric(self):
        h, clock = health()
        h.start_session()
        clock.advance(1.2)
        h.note_observation(queue_ms=5, infer_ms=20)
        self.assertEqual(h.state, 'READY')
        self.assertTrue(h.ready)
        self.assertIsNone(h.recovery_s)      # 冷启动不算"恢复"

    def test_warmup_timeout_is_offline_not_silently_warm(self):
        h, clock = health()
        h.start_session()
        clock.advance(24)
        self.assertEqual(h.refresh(), 'WARMING')
        clock.advance(2)
        self.assertEqual(h.refresh(), 'OFFLINE')
        self.assertEqual(h.transitions[-1][2], 'warmup_timeout')

    def test_observation_gap_flips_to_offline_and_back_with_recovery_time(self):
        h, clock = health()
        h.start_session()
        h.note_observation(queue_ms=5, infer_ms=20)
        clock.advance(3)
        h.note_observation(queue_ms=5, infer_ms=20)
        clock.advance(7)                     # 超过 offline_after_s=6
        self.assertEqual(h.refresh(), 'OFFLINE')
        offline_mark = clock()
        clock.advance(4.5)
        h.note_observation(queue_ms=5, infer_ms=20)
        self.assertEqual(h.state, 'READY')
        self.assertAlmostEqual(h.recovery_s, 4.5, places=3)

    def test_slow_results_require_a_streak_before_degraded(self):
        h, clock = health()
        h.start_session()
        h.note_observation(queue_ms=5, infer_ms=20)
        for _ in range(2):
            h.note_observation(queue_ms=200, infer_ms=20)
            self.assertEqual(h.state, 'READY')
        h.note_observation(queue_ms=200, infer_ms=20)
        self.assertEqual(h.state, 'DEGRADED')
        self.assertTrue(h.ready)             # 降级仍是"能出结果"
        h.note_observation(queue_ms=5, infer_ms=20)
        self.assertEqual(h.state, 'READY')

    def test_streak_resets_on_a_fast_result(self):
        h, clock = health()
        h.start_session()
        h.note_observation()
        h.note_observation(queue_ms=200)
        h.note_observation(queue_ms=200)
        h.note_observation(queue_ms=5)       # 清零
        h.note_observation(queue_ms=200)
        h.note_observation(queue_ms=200)
        self.assertEqual(h.state, 'READY')

    def test_snapshot_is_json_safe_and_reports_seconds(self):
        h, clock = health()
        h.start_session()
        h.note_observation(queue_ms=5, infer_ms=20)
        clock.advance(2)
        snap = h.snapshot()
        self.assertEqual(snap['state'], 'READY')
        self.assertTrue(snap['ready'])
        self.assertAlmostEqual(snap['last_seen_s'], 2.0, places=3)
        self.assertEqual([t['to'] for t in snap['transitions']], ['WARMING', 'READY'])
        self.assertAlmostEqual(snap['transitions'][-1]['at'], 0.0, places=3)

    def test_degraded_does_not_hide_a_real_outage(self):
        h, clock = health()
        h.start_session()
        for _ in range(3):
            h.note_observation(queue_ms=300)
        self.assertEqual(h.state, 'DEGRADED')
        clock.advance(7)
        self.assertEqual(h.refresh(), 'OFFLINE')   # 降级不等于在线


if __name__ == '__main__':
    unittest.main()
