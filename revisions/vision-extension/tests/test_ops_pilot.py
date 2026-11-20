from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ops_pilot import reconciliation, sensor_state


class OpsPilotTests(unittest.TestCase):
    def test_reconciliation_classifies_stale_and_mismatched_sessions(self):
        sample = {"probes": {"dataset": {"ok": True}, "preview": {"ok": True}},
                  "preview_run_id": "run-1", "dataset_run_id": "run-1", "dataset_generation": 6}
        control = {"active": {"run_id": "run-1", "generation": 6},
                   "session": {"heartbeat_age_s": 2, "lease_ttl_s": 120}}
        self.assertEqual(reconciliation(sample, control), "running_aligned")
        control["session"]["heartbeat_age_s"] = 100
        self.assertTrue(reconciliation(sample, control).endswith("heartbeat_missing_or_stale"))
        control["session"]["heartbeat_age_s"] = 2
        sample["dataset_generation"] = 5
        self.assertTrue(reconciliation(sample, control).endswith("generation_disagrees"))
        sample["dataset_run_id"] = "other"
        self.assertTrue(reconciliation(sample, control).endswith("run_ids_disagree"))
        control["active"] = None
        self.assertTrue(reconciliation(sample, control).endswith("control_idle_with_pi_run"))
        sample.update(preview_run_id=None, dataset_run_id=None)
        self.assertEqual(reconciliation(sample, control), "idle")
        sample["probes"]["dataset"]["ok"] = False
        self.assertEqual(reconciliation(sample, control), "dataset_unavailable")

    def test_sensor_freshness_uses_each_configured_limit(self):
        reading = {"present": True, "stale": False, "quality": "ok", "sensor_id": "scd41",
                   "value": 400, "age_ms": 19_999}
        body = {"fields": {"co2_ppm": reading}, "stale_after_ms": {"co2_ppm": 20_000}}
        self.assertTrue(sensor_state(body)["co2_ppm"]["fresh"])
        reading["age_ms"] = 20_001
        self.assertFalse(sensor_state(body)["co2_ppm"]["fresh"])
        self.assertFalse(sensor_state(body)["temperature_c"]["fresh"])


if __name__ == "__main__":
    unittest.main()
