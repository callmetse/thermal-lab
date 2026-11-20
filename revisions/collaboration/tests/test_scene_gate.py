"""Unit tests for :mod:`lab.scene_gate`.

These tests pin the *safety* properties of the empty-scene gate as much as
the byte-saving ones: the gate must never drop a frame while a task is
active, and a frame with real motion must always be allowed.

Time is fully fake (a writable clock callable), so nothing here sleeps.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.scene_gate import (  # noqa: E402  (path setup must run first)
    REASONS,
    STATES,
    SceneGate,
)

FRAME_SHAPE = (480, 640)
QUIET_VALUE = 100


class FakeClock:
    """Writable fake clock; the module must only ever read ``clock()``."""

    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta
        return self.now


def quiet_frame(value=QUIET_VALUE, shape=FRAME_SHAPE):
    return np.full(shape, value, dtype=np.uint8)


def flipped_frame(shape=FRAME_SHAPE):
    """Full-screen inversion: every pixel changes by far more than the threshold."""
    return np.full(shape, 255 - QUIET_VALUE, dtype=np.uint8)


def reach_empty_verified(gate, clock, step=0.05, limit=200):
    """Drive *gate* with quiet frames until it reports ``empty_verified``.

    Returns the list of decisions taken, ended by the promoting frame.
    """
    gate.note_evidence("EMPTY", now=clock())
    decisions = [gate.decide(quiet_frame(), now=clock())]
    for _ in range(limit):
        if decisions[-1]["state"] == "empty_verified":
            return decisions
        clock.advance(step)
        decisions.append(gate.decide(quiet_frame(), now=clock()))
    raise AssertionError("gate never reached empty_verified")


class SceneGateTests(unittest.TestCase):
    # 1. -------------------------------------------------------------- first frame
    def test_first_frame_is_allowed(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        decision = gate.decide(quiet_frame(), now=clock())

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "first_frame")
        self.assertEqual(decision["state"], "uncertain")
        self.assertEqual(decision["changed_ratio"], 0.0)
        self.assertIsNone(decision["since_audit_s"])
        self.assertIsNone(decision["since_last_allow_s"])
        self.assertIn(decision["reason"], REASONS)
        self.assertEqual(gate.state, "uncertain")

    # 2. --------------------------------- quiet streak -> empty_verified, then skip
    def test_quiet_streak_verifies_empty_then_skips(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)  # confirm_quiet_frames defaults to 12
        decisions = reach_empty_verified(gate, clock)

        # 1 first_frame + exactly confirm_quiet_frames quiet frames.
        self.assertEqual(len(decisions), 13)
        self.assertEqual(decisions[0]["reason"], "first_frame")
        self.assertEqual(decisions[-1]["reason"], "uncertain_observe")
        self.assertEqual(decisions[-1]["state"], "empty_verified")
        self.assertTrue(all(decision["allowed"] for decision in decisions))

        clock.advance(0.05)
        decision = gate.decide(quiet_frame(), now=clock())
        self.assertFalse(decision["allowed"], "verified-empty scene should be skipped")
        self.assertEqual(decision["reason"], "empty_scene_skip")
        self.assertEqual(decision["state"], "empty_verified")
        self.assertIsNotNone(decision["since_audit_s"])
        self.assertEqual(decision["since_audit_s"], decision["since_last_allow_s"])

    # 3. --------------------------------------------------- audit_due, then skip again
    def test_audit_due_allows_then_skips_again(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        reach_empty_verified(gate, clock)
        promoted_at = clock()

        skipped = 0
        audit = None
        for _ in range(80):
            clock.advance(0.05)
            decision = gate.decide(quiet_frame(), now=clock())
            if decision["reason"] == "empty_scene_skip":
                self.assertFalse(decision["allowed"])
                skipped += 1
            if decision["reason"] == "audit_due":
                audit = decision
                break

        self.assertIsNotNone(audit, "audit_interval_s must eventually allow a frame")
        self.assertTrue(audit["allowed"])
        self.assertEqual(audit["state"], "empty_verified")
        self.assertGreater(skipped, 0, "frames between audits must be skipped")

        elapsed = clock() - promoted_at
        self.assertGreaterEqual(elapsed + 1e-9, 2.0, "audit must wait for audit_interval_s")
        self.assertLess(elapsed, 2.2)

        # Right after the audit the gate closes again.
        clock.advance(0.05)
        decision = gate.decide(quiet_frame(), now=clock())
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "empty_scene_skip")

    # 4. ------------------------------------------------------- sudden full-screen flip
    def test_sudden_change_marks_active_and_allows(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        gate.decide(quiet_frame(), now=clock())
        clock.advance(0.05)

        decision = gate.decide(flipped_frame(), now=clock())
        self.assertEqual(decision["state"], "active")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "active_observe")
        self.assertGreaterEqual(decision["changed_ratio"], 0.02)
        self.assertEqual(decision["changed_ratio"], 1.0)
        self.assertEqual(gate.state, "active")

    # 5. -------------------------------------------------------- task guard wins
    def test_task_active_guards_action_window(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        reach_empty_verified(gate, clock)

        # Same timestamp twice: the second call would be min_gap at best, but a
        # running task must always get its frame.
        for attempt in range(2):
            decision = gate.decide(quiet_frame(), now=clock(), task_active=True)
            self.assertTrue(decision["allowed"], f"frame {attempt} dropped during a task")
            self.assertEqual(decision["reason"], "task_guard")
            self.assertEqual(decision["state"], "active")
            self.assertEqual(gate.state, "active")

        # And while the task runs, no frame is ever skipped even if quiet.
        for _ in range(6):
            clock.advance(0.05)
            decision = gate.decide(quiet_frame(), now=clock(), task_active=True)
            self.assertTrue(decision["allowed"])
            self.assertEqual(decision["reason"], "task_guard")
            self.assertNotIn(decision["reason"], {"empty_scene_skip", "min_gap"})

    # 6. ------------------------------------------------------------------- min gap
    def test_min_gap_blocks_second_call_at_same_instant(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)  # sample_fps=20 -> min_gap 0.05 s
        reach_empty_verified(gate, clock)

        clock.advance(2.0)  # long enough for an audit
        first = gate.decide(quiet_frame(), now=clock())
        self.assertTrue(first["allowed"])
        self.assertEqual(first["reason"], "audit_due")

        second = gate.decide(quiet_frame(), now=clock())  # clock did not move
        self.assertFalse(second["allowed"])
        self.assertEqual(second["reason"], "min_gap")
        self.assertLess(second["since_last_allow_s"], 0.05)

        # The floor follows sample_fps: 10 fps -> 0.1 s, so a 0.06 s gap is tight.
        slow_clock = FakeClock()
        slow_gate = SceneGate(clock=slow_clock, sample_fps=10.0)
        self.assertEqual(slow_gate.config["min_gap_s"], 0.1)
        reach_empty_verified(slow_gate, slow_clock)
        slow_clock.advance(2.0)
        self.assertTrue(slow_gate.decide(quiet_frame(), now=slow_clock())["allowed"])
        slow_clock.advance(0.06)
        decision = slow_gate.decide(quiet_frame(), now=slow_clock())
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "min_gap")

    # 7. -------------------------------------------- shape changes and odd dtypes
    def test_shape_change_and_non_uint8_do_not_raise(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        gate.decide(quiet_frame(), now=clock())

        clock.advance(0.05)
        # Different geometry: behaves like "no previous frame", not an exception.
        decision = gate.decide(quiet_frame(shape=(240, 320)), now=clock())
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "first_frame")

        odd_inputs = [
            np.full(FRAME_SHAPE, 300.0, dtype=np.float64),   # clipped to 255
            np.full(FRAME_SHAPE, -5, dtype=np.int32),        # clipped to 0
            np.zeros((0, 0), dtype=np.uint8),                # empty array
            np.array([[7]], dtype=np.uint8),                 # 1x1 image
            np.full((48, 64, 3), 9, dtype=np.uint8),         # 3-D input
            np.zeros((3, 5), dtype=bool),                    # bool mask
            np.full((7, 9), np.nan, dtype=np.float32),       # NaN pixels
        ]
        for arr in odd_inputs:
            clock.advance(0.05)
            decision = gate.decide(arr, now=clock())
            self.assertIsInstance(decision, dict)
            self.assertIn(decision["reason"], REASONS)
            self.assertIn(decision["state"], STATES)
            self.assertIsInstance(decision["allowed"], bool)
            self.assertIsInstance(decision["changed_ratio"], float)

        # A tiny but stable 1x1 stream keeps working (no division by zero).
        tiny = np.array([[7]], dtype=np.uint8)
        clock.advance(0.05)
        self.assertIn(gate.decide(tiny, now=clock())["reason"], REASONS)

    # 8. --------------------------------------------------------- stats coherence
    def test_stats_are_consistent(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        gate.note_evidence("EMPTY", now=clock())      # FIX-03：需要外部空场景证据

        events = []
        t = 0.0
        events.append((t, quiet_frame()))            # first_frame
        for _ in range(12):                          # quiet streak -> empty_verified
            t += 0.05
            events.append((t, quiet_frame()))
        for _ in range(5):                           # skipped by the audit cadence
            t += 0.05
            events.append((t, quiet_frame()))
        t += 2.0                                     # audit is due
        events.append((t, quiet_frame()))
        t += 0.05
        events.append((t, flipped_frame()))          # motion -> active
        t += 0.05
        events.append((t, flipped_frame()))

        for timestamp, frame in events:
            decision = gate.decide(frame, now=timestamp, task_active=False)
            self.assertIn(decision["reason"], REASONS)
            self.assertIn(decision["state"], STATES)

        stats = gate.stats
        self.assertEqual(stats["frames_seen"], len(events))
        self.assertEqual(stats["frames_seen"], stats["allowed"] + stats["skipped"])
        self.assertEqual(sum(stats["by_reason"].values()), stats["frames_seen"])
        self.assertEqual(sum(stats["by_state"].values()), stats["frames_seen"])
        self.assertGreater(stats["skipped"], 0, "some frames must have been skipped")
        self.assertLessEqual(set(stats["by_reason"]), set(REASONS))
        self.assertLessEqual(set(stats["by_state"]), set(STATES))
        self.assertIn("first_frame", stats["by_reason"])

        # reset() forgets everything, including counters.
        gate.reset()
        cleared = gate.stats
        self.assertEqual(cleared["frames_seen"], 0)
        self.assertEqual(cleared["allowed"], 0)
        self.assertEqual(cleared["skipped"], 0)
        self.assertEqual(cleared["by_reason"], {})
        self.assertEqual(cleared["by_state"], {})
        self.assertEqual(gate.state, "uncertain")
        clock.advance(0.05)
        self.assertEqual(gate.decide(quiet_frame(), now=clock())["reason"], "first_frame")


class EvidenceGatingTests(unittest.TestCase):
    """FIX-03：没有外部证据时，安静帧不得把门控推进到 empty_verified。"""

    def test_without_evidence_it_never_claims_empty(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        allowed = 0
        for _ in range(200):
            allowed += gate.decide(quiet_frame(), now=clock())["allowed"]
            clock.advance(0.05)
        self.assertNotEqual(gate.state, "empty_verified")
        self.assertEqual(allowed, 200)          # 保守默认：全部放行，不误拦

    def test_present_evidence_forces_active(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        reach_empty_verified(gate, clock)
        self.assertEqual(gate.state, "empty_verified")
        gate.note_evidence("PRESENT", now=clock())
        self.assertEqual(gate.state, "active")
        self.assertTrue(gate.decide(quiet_frame(), now=clock.advance(0.05))["allowed"])

    def test_evidence_expiry_reopens_the_gate(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock, config={"evidence_ttl_s": 2.0})
        reach_empty_verified(gate, clock)
        self.assertEqual(gate.state, "empty_verified")
        decision = None
        for _ in range(60):
            clock.advance(0.1)
            decision = gate.decide(quiet_frame(), now=clock())
        self.assertIn(decision["state"], ("uncertain", "active"))
        self.assertTrue(decision["allowed"])

    def test_epoch_change_invalidates_evidence(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        gate.set_epoch("run-a")
        gate.note_evidence("EMPTY", now=clock(), epoch="run-a")
        for _ in range(40):
            clock.advance(0.05)
            gate.decide(quiet_frame(), now=clock())
        self.assertEqual(gate.state, "empty_verified")
        gate.set_epoch("run-b")                 # 新会话：旧证据作废
        self.assertEqual(gate.state, "uncertain")
        self.assertTrue(gate.decide(quiet_frame(), now=clock.advance(0.05))["allowed"])

    def test_unknown_evidence_withdraws_verification(self):
        clock = FakeClock()
        gate = SceneGate(clock=clock)
        reach_empty_verified(gate, clock)
        gate.note_evidence("UNKNOWN", now=clock())
        self.assertEqual(gate.state, "uncertain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
