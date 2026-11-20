import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.hard_examples import HardExampleSelector


class FakeClock:
    def __init__(self, start=100.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def selector(**overrides):
    clock = FakeClock()
    config = {'enabled': True, 'random_audit_interval_s': 30.0, 'min_gap_s': 5.0,
              'motion_ratio_threshold': 0.02, 'motion_window_s': 3.0,
              'sampling_probability': 0.02}
    config.update(overrides)
    return HardExampleSelector(config, clock=clock), clock


class SelectorTests(unittest.TestCase):
    def test_disabled_never_captures(self):
        s, clock = selector(enabled=False)
        self.assertFalse(s.consider(now=clock(), changed_ratio=0.5)['capture'])

    def test_random_audit_respects_interval_and_min_gap(self):
        s, clock = selector(random_audit_interval_s=30.0, min_gap_s=5.0)
        first = s.consider(now=clock())
        self.assertTrue(first['capture'])
        self.assertEqual(first['reasons'], ['random_audit'])
        self.assertFalse(s.consider(now=clock.advance(10))['capture'])   # 未到 30 s
        clock.advance(20)                                                # 到 30 s
        self.assertTrue(s.consider(now=clock())['capture'])

    def test_min_gap_suppresses_bursts(self):
        s, clock = selector(random_audit_interval_s=1.0, min_gap_s=10.0)
        self.assertTrue(s.consider(now=clock())['capture'])
        clock.advance(2)
        self.assertFalse(s.consider(now=clock(), changed_ratio=0.5)['capture'])

    def test_motion_without_person_is_a_miss_candidate(self):
        s, clock = selector(random_audit_interval_s=300.0)
        s.note_observation(0, now=clock())
        decision = s.consider(now=clock.advance(1), changed_ratio=0.05)
        self.assertTrue(decision['capture'])
        self.assertIn('motion_without_person', decision['reasons'])

    def test_motion_with_person_is_not_a_miss_candidate(self):
        s, clock = selector(random_audit_interval_s=300.0, min_gap_s=0.0)
        s.note_observation(1, now=clock())
        s.consider(now=clock())                        # 先吃掉一次随机审计
        self.assertFalse(s.consider(now=clock.advance(1), changed_ratio=0.05)['capture'])

    def test_stale_observation_does_not_trigger(self):
        s, clock = selector(random_audit_interval_s=300.0, motion_window_s=2.0, min_gap_s=0.0)
        s.note_observation(0, now=clock())
        s.consider(now=clock())                        # 吃掉随机审计
        self.assertFalse(s.consider(now=clock.advance(10), changed_ratio=0.05)['capture'])

    def test_gate_negative_with_motion_is_flagged(self):
        s, clock = selector(random_audit_interval_s=300.0)
        s.note_observation(1, now=clock())
        decision = s.consider(now=clock.advance(1), changed_ratio=0.1,
                              gate_state='negative', arm='B')
        self.assertTrue(decision['capture'])
        self.assertIn('gate_negative_but_motion', decision['reasons'])

    def test_sampling_probability_is_recorded(self):
        s, clock = selector(sampling_probability=0.05)
        decision = s.consider(now=clock())
        self.assertAlmostEqual(decision['sampling_probability'], 0.05)


if __name__ == '__main__':
    unittest.main()
