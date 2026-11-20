import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from collaboration_mode import apply_mode


BASE = {"project": "abc20261102",
        "collaboration": {"enabled": True, "mode": "active", "media_budget_bps": 4000000}}


class ApplyModeTests(unittest.TestCase):
    def test_shadow_keeps_budget_but_stops_enforcing(self):
        config, error = apply_mode(json.loads(json.dumps(BASE)), "shadow")
        self.assertIsNone(error)
        block = config["collaboration"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["mode"], "shadow")
        self.assertEqual(block["media_budget_bps"], 4000000)   # 影子仍按同一预算预测

    def test_off_does_not_touch_budget(self):
        config, error = apply_mode(json.loads(json.dumps(BASE)), "off")
        self.assertIsNone(error)
        self.assertFalse(config["collaboration"]["enabled"])
        self.assertEqual(config["collaboration"]["media_budget_bps"], 4000000)

    def test_active_requires_explicit_budget(self):
        config, error = apply_mode({"collaboration": {"enabled": False}}, "active")
        self.assertIsNotNone(error)
        self.assertIn("预算", error)

    def test_active_with_budget_is_accepted(self):
        config, error = apply_mode({"collaboration": {}}, "active", 3000000)
        self.assertIsNone(error)
        self.assertEqual(config["collaboration"]["mode"], "active")
        self.assertEqual(config["collaboration"]["media_budget_bps"], 3000000)

    def test_active_falls_back_to_existing_budget(self):
        config, error = apply_mode(json.loads(json.dumps(BASE)), "active")
        self.assertIsNone(error)
        self.assertEqual(config["collaboration"]["mode"], "active")

    def test_unknown_mode_is_rejected(self):
        _, error = apply_mode(json.loads(json.dumps(BASE)), "passthrough")
        self.assertIsNotNone(error)

    def test_defaults_are_filled_for_a_bare_block(self):
        config, error = apply_mode({"collaboration": {}}, "shadow")
        self.assertIsNone(error)
        self.assertEqual(config["collaboration"]["alpha"], 0.8)
        self.assertEqual(config["collaboration"]["action_floor_fps"], 8)
        self.assertTrue(config["collaboration"]["pacing"])

    def test_does_not_mutate_input(self):
        original = json.loads(json.dumps(BASE))
        apply_mode(original, "off")
        self.assertEqual(original, BASE)


if __name__ == "__main__":
    unittest.main()
