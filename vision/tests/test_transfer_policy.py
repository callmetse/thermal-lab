import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.transfer_policy import MediaBudgetPolicy, TierSelector, TokenBucket


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def policy(**overrides):
    clock = overrides.pop('clock', None) or FakeClock()
    config = {'enabled': True, 'mode': 'active', 'media_budget_bps': 800000,
              'alpha': 0.8, 'burst_s': 0.25, 'action_floor_fps': 8.0,
              'pacing': False, 'quality_tiers': [], 'deadline_ms': 500.0,
              'max_queue_items': 1, 'max_queue_bytes': 120000,
              'offline_probe_interval_s': 3.0}
    config.update(overrides)
    return MediaBudgetPolicy(config, clock=clock), clock


class TokenBucketTests(unittest.TestCase):
    def test_refill_is_capped_by_capacity(self):
        clock = FakeClock()
        bucket = TokenBucket(1000.0, burst_s=0.2, clock=clock)   # 200 B 容量
        self.assertEqual(bucket.capacity, 200.0)
        clock.advance(10)
        bucket.take(200, clock())
        self.assertAlmostEqual(bucket.tokens, 0.0, places=6)
        clock.advance(100)
        bucket.take(1, clock())
        self.assertLessEqual(bucket.tokens, bucket.capacity)

    def test_insufficient_tokens_are_not_deducted(self):
        clock = FakeClock()
        bucket = TokenBucket(1000.0, burst_s=0.2, clock=clock)
        self.assertFalse(bucket.take(201, clock()))
        self.assertAlmostEqual(bucket.tokens, 200.0, places=6)


class AdmissionTests(unittest.TestCase):
    def test_budget_exhaustion_reports_byte_budget(self):
        p, clock = policy(media_budget_bps=80000)   # 8000 B/s，突发容量 2000 B
        first = p.decide(1500, clock())
        self.assertTrue(first['allowed'])
        denied = 0
        for _ in range(5):
            d = p.decide(1500, clock.advance(0.1))
            denied += not d['allowed']
            if not d['allowed']:
                self.assertEqual(d['reason'], 'byte_budget')
        self.assertGreater(denied, 0)
        self.assertEqual(p.dropped['byte_budget'], denied)

    def test_hard_budget_invariant_cannot_be_bypassed_by_action_floor(self):
        """FIX-02：动作底线不得绕过硬预算。20 s、预算 100 kB/s、帧 6 kB、20 fps。"""
        p, clock = policy(media_budget_bps=800000, pacing=True, action_floor_fps=8.0)
        p.start_session(20, clock())
        admitted = 0
        for i in range(400):
            now = clock.advance(0.05) if i else clock()
            p.observe_frame(6000, now)
            if p.decide(6000, now)['allowed']:
                p.record_sent(6000, now)
                admitted += 6000
        rate = 800000 * 0.8 / 8
        burst = max(rate * 0.25, 6000 * 1.1)
        self.assertLessEqual(admitted, rate * 20 + burst + 1)

    def test_below_action_floor_declares_insufficient_sampling_instead_of_overspending(self):
        p, clock = policy(media_budget_bps=200000, pacing=True, action_floor_fps=8.0)
        p.start_session(20, clock())
        for _ in range(10):
            p.observe_frame(26800)
        target = p.target_fps_value(clock())
        self.assertLess(target, 8.0)                 # 不再被抬到底线
        self.assertTrue(p.insufficient_sampling)
        self.assertTrue(p.decide(1000, clock())['insufficient_sampling'])

    def test_task_active_does_not_unlock_the_budget(self):
        p, clock = policy(media_budget_bps=80000)
        p.decide(1500, clock())
        d = p.decide(1500, clock(), task_active=True)
        self.assertFalse(d['allowed'])
        self.assertIn(d['reason'], ('byte_budget', 'queue_full'))

    def test_expired_frame_is_dropped_before_budget(self):
        p, clock = policy()
        d = p.decide(1000, clock(), age_s=0.9)      # deadline 500 ms
        self.assertFalse(d['allowed'])
        self.assertEqual(d['reason'], 'expired')

    def test_queue_caps_always_apply(self):
        p, clock = policy()
        self.assertEqual(p.decide(1000, clock(), queue_items=1)['reason'], 'queue_full')
        self.assertEqual(p.decide(1000, clock(), queue_bytes=120000)['reason'], 'queue_bytes')

    def test_offline_allows_bounded_probes_only(self):
        """FIX-01：离线时必须还能发出有限探针，否则永远收不到结果、状态卡死。"""
        p, clock = policy()
        first = p.decide(1000, clock(), gpu_ready=False)
        self.assertTrue(first['allowed'])
        self.assertEqual(first['reason'], 'offline_probe')
        second = p.decide(1000, clock(), gpu_ready=False)
        self.assertFalse(second['allowed'])
        self.assertEqual(second['reason'], 'offline')
        clock.advance(2.9)
        self.assertEqual(p.decide(1000, clock(), gpu_ready=False)['reason'], 'offline')
        clock.advance(0.2)
        self.assertEqual(p.decide(1000, clock(), gpu_ready=False)['reason'], 'offline_probe')
        self.assertEqual(p.offline_probes, 2)

    def test_shadow_is_not_enforced_but_still_judged(self):
        p, clock = policy(mode='shadow', media_budget_bps=80000)
        decisions = [p.decide(1500, clock()) for _ in range(3)]
        self.assertFalse(decisions[-1]['enforced'])
        self.assertFalse(decisions[-1]['allowed'])
        self.assertEqual(decisions[-1]['reason'], 'byte_budget')

    def test_disabled_policy_always_allows(self):
        p, clock = policy(enabled=False, mode='off', media_budget_bps=0)
        d = p.decide(10 ** 7, clock())
        self.assertTrue(d['allowed'])
        self.assertFalse(d['enforced'])

    def test_zero_budget_does_not_take_control(self):
        p, clock = policy(media_budget_bps=0)
        self.assertFalse(p.active_control)
        self.assertTrue(p.decide(10 ** 7, clock())['allowed'])

    def test_summary_is_self_consistent(self):
        p, clock = policy(media_budget_bps=80000)
        allowed = 0
        for _ in range(6):
            if p.decide(1500, clock())['allowed']:
                allowed += 1
                p.record_sent(1500, clock())
            clock.advance(0.1)
        s = p.summary(duration_s=1.0)
        self.assertEqual(s['would_frames'], allowed)
        self.assertEqual(s['sent_frames'], allowed)
        self.assertEqual(s['would_frames'] + s['dropped_total'], s['decisions'])
        self.assertEqual(sum(s['dropped'].values()), s['dropped_total'])


class SessionIsolationTests(unittest.TestCase):
    def test_second_session_starts_from_zero_and_process_totals_accumulate(self):
        """FIX-05：同一进程连续两轮，第二轮计数不得混入上一轮。"""
        p, clock = policy(media_budget_bps=800000, pacing=False)
        p.start_session(20, clock())
        for _ in range(5):
            if p.decide(1000, clock())['allowed']:
                p.record_sent(1000, clock())
            clock.advance(0.05)
        first_sent = p.sent_frames
        p.start_session(20, clock.advance(1))
        self.assertEqual(p.sent_frames, 0)
        self.assertEqual(p.dropped.total(), 0)
        empty = p.summary(duration_s=1.0)
        self.assertEqual(empty['sent_frames'], 0)
        self.assertEqual(empty['process_totals']['sent_frames'], first_sent)


class PacingTests(unittest.TestCase):
    def paced_policy(self, **overrides):
        clock = FakeClock()
        config = {'enabled': True, 'mode': 'active', 'media_budget_bps': 4000000,
                  'alpha': 0.8, 'pacing': True, 'action_floor_fps': 8.0,
                  'quality_tiers': [], 'target_fps_round': 0.5}
        config.update(overrides)
        return MediaBudgetPolicy(config, clock=clock), clock

    def test_target_fps_derives_from_budget_and_frame_size(self):
        p, clock = self.paced_policy()                 # 3.2 Mbps = 400 kB/s
        p.start_session(20)
        for _ in range(10):
            p.observe_frame(26800)
        self.assertEqual(p.target_fps_value(clock()), 14.5)   # 400000/26800≈14.9→14.5

    def test_average_rate_matches_target_without_quantising_to_capture_grid(self):
        p, clock = self.paced_policy()
        p.start_session(20)
        for _ in range(10):
            p.observe_frame(26800)
        allowed = 0
        for i in range(400):
            now = i * 0.05
            p.observe_frame(26800, now)
            if p.decide(26800, now)['allowed']:
                allowed += 1
                p.record_sent(26800, now)
        rate = allowed / 20.0
        self.assertGreater(rate, 12.5)
        self.assertLessEqual(rate, 15.5)
        self.assertEqual(p.target_fps, 14.5)

    def test_alternating_capture_grid_does_not_halve_the_rate(self):
        p, clock = self.paced_policy()
        p.start_session(20)
        for _ in range(10):
            p.observe_frame(26800)
        allowed = 0
        t = 0.0
        pattern = [0.0333, 0.0667]
        for i in range(400):
            t += pattern[i % 2] if i else 0.0
            p.observe_frame(26800, t)
            if p.decide(26800, t)['allowed']:
                allowed += 1
                p.record_sent(26800, t)
        self.assertGreater(allowed / t, 13.0)          # 不是 10 fps
        self.assertLess(allowed / t, 15.5)

    def test_shadow_marks_pacing_but_stays_unenforced(self):
        p, clock = self.paced_policy(mode='shadow')
        p.start_session(20)
        for _ in range(10):
            p.observe_frame(26800)
        allowed = 0
        for i in range(100):
            allowed += p.decide(26800, i * 0.05)['allowed']
        self.assertLess(allowed, 100)
        self.assertFalse(p.decide(26800, 10.0)['enforced'])


class TierTests(unittest.TestCase):
    def test_hysteresis_and_dwell(self):
        clock = FakeClock()
        tiers = TierSelector([55, 50, 45], up_ratio=0.6, down_ratio=0.92, dwell_s=5.0, clock=clock)
        self.assertEqual(tiers.current(), 55)
        tiers.update(1.0, clock.advance(1))
        self.assertEqual(tiers.current(), 55)
        tiers.update(1.0, clock.advance(5))
        self.assertEqual(tiers.current(), 50)
        tiers.update(0.1, clock.advance(2))
        self.assertEqual(tiers.current(), 50)
        tiers.update(0.1, clock.advance(6))
        self.assertEqual(tiers.current(), 55)
        self.assertEqual(tiers.changes, 2)

    def test_policy_quality_none_when_no_tiers(self):
        p, clock = policy()
        self.assertIsNone(p.quality(clock()))


if __name__ == '__main__':
    unittest.main()
