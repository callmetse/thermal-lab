"""Per-session endpoint choice: precision-first switching between smart and fixed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:                                     # sherpa-onnx only exists on the GPU host
    import worker as worker_module  # noqa: E402
except Exception as exc:                 # noqa: BLE001
    worker_module = None
    IMPORT_ERROR = str(exc)


def stub(mode, endpoint_present):
    w = worker_module.Worker.__new__(worker_module.Worker)
    w.ep_mode = mode
    w.endpoint = object() if endpoint_present else None
    return w


@unittest.skipIf(worker_module is None, 'worker needs sherpa-onnx (GPU host only)')
class EndpointChoice(unittest.TestCase):
    def test_auto_single_uses_semantic_merge(self):
        self.assertTrue(stub('auto', True).resolve_endpoint('single'))

    def test_auto_multi_keeps_fixed_window(self):
        self.assertFalse(stub('auto', True).resolve_endpoint('multi'))

    def test_auto_defaults_to_single(self):
        self.assertTrue(stub('auto', True).resolve_endpoint(None))

    def test_missing_model_never_merges(self):
        self.assertFalse(stub('auto', False).resolve_endpoint('single'))
        self.assertFalse(stub('smart', False).resolve_endpoint('single'))

    def test_forced_modes_ignore_session(self):
        self.assertTrue(stub('smart', True).resolve_endpoint('multi'))
        self.assertFalse(stub('fixed', True).resolve_endpoint('single'))


if __name__ == '__main__':
    unittest.main()
