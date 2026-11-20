"""P1-02/03 worker 侧协议单测：命令校验、会话状态、租约到期停机。

不连 MQTT：用 object.__new__ 造一个 Worker 壳，替换模块级 publish，
所有断言只看"发出了什么"。
"""
import sys
import queue
import threading
import time
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lab.worker as worker_mod  # noqa: E402
from lab.common import accepted_command  # noqa: E402

C = {"project": "abc20261102", "capture_fps": 30, "sample_fps": 20}


def _cmd(**kw):
    base = {"project": "abc20261102", "issued_ms": time.time() * 1000}
    base.update(kw)
    return base


def test_command_matrix():
    ok = [
        _cmd(op="start", run_id="acc-1", arm="A", source="camera", seconds=10),
        _cmd(op="start", run_id="acc-1", arm="A", source="camera", seconds=0, unlimited=True),
        _cmd(op="stop", run_id="acc-1"),
        _cmd(op="lease", run_id="acc-1", ttl_s=120, generation=4),
        _cmd(op="session_query"),
    ]
    bad = [
        _cmd(op="reboot", run_id="acc-1"),
        _cmd(op="lease", run_id="acc-1", ttl_s=0),
        _cmd(op="lease", run_id="../etc", ttl_s=60),
        _cmd(op="lease", run_id="acc-1", generation="x"),
        _cmd(op="start", run_id="acc-1", arm="Z", source="camera", seconds=10),
        _cmd(op="start", run_id="acc-1", arm="A", source="camera", seconds=10, generation=-1),
        {"project": "other", "op": "session_query"},
    ]
    for body in ok:
        assert accepted_command(body, C), f"应接受: {body}"
    for body in bad:
        assert not accepted_command(body, C), f"应拒绝: {body}"


def make_shell(published):
    w = object.__new__(worker_mod.Worker)
    w.c = {"project": "abc20261102"}
    w.root = "labs/abc20261102/v1"
    w.role = "edge"
    w.session_state = "idle"
    w.session_run = None
    w.generation = 0
    w.lease_expires = 0.0
    w.lease_ttl = 120.0
    w.session_started = None
    w.session_config = None
    w.stop = threading.Event()
    w.client = object()
    return w


def test_publish_and_state(published):
    w = make_shell(published)
    w.session_run = "acc-1"
    w.set_session("running")
    assert published[-1][0].endswith("/runs/acc-1/events")
    body = published[-1][1]
    assert body["kind"] == "session_state" and body["state"] == "running"
    assert body["generation"] == 0 and "ts_ms" in body


def test_lease_expiry_stops(published):
    w = make_shell(published)
    w.session_run = "acc-1"
    w.session_started = time.monotonic()
    w.lease_expires = time.monotonic() - 1.0      # 已过期
    cancel = threading.Event()
    w.lease_guard({"run_id": "acc-1"}, cancel)
    assert cancel.is_set(), "租约过期应取消会话"
    assert w.session_state == "interrupted", w.session_state
    body = published[-1][1]
    assert body["reason"] == "lease_expired"


def test_lease_renewal_keeps_running(published):
    w = make_shell(published)
    w.session_run = "acc-1"
    w.lease_expires = time.monotonic() + 0.3
    cancel = threading.Event()

    def renew():
        time.sleep(0.15)
        w.lease_expires = time.monotonic() + 1.0

    threading.Thread(target=renew, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(0.6), cancel.set()), daemon=True).start()
    w.lease_guard({"run_id": "acc-1"}, cancel)
    assert w.session_state != "interrupted", "续租后不应停机"


def test_cloud_retains_newer_start_during_previous_cleanup(published):
    worker = make_shell(published)
    worker.role = "cloud"
    worker.commands = queue.Queue(8)
    worker.frames = queue.Queue(1)
    worker.client = Mock()
    worker.media_client = None
    worker.local_preview = None
    worker.dataset_archive = None
    old_started = threading.Event()
    old_canceled = threading.Event()
    cleanup = threading.Event()
    new_started = threading.Event()
    seen = []

    def session(run, cancel):
        seen.append((run["run_id"], worker.generation))
        if run["run_id"] == "replay":
            old_started.set()
            if cancel.wait(2):
                old_canceled.set()
            cleanup.wait(2)
        else:
            new_started.set()

    worker.session = session
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        worker.commands.put({"op": "start", "run_id": "replay", "generation": 11})
        assert old_started.wait(1)
        worker.commands.put({"op": "start", "run_id": "camera", "generation": 12})
        assert old_canceled.wait(1), "A newer start should cancel the old cloud session"
        assert not new_started.is_set()
        cleanup.set()
        assert new_started.wait(1), "The new cloud session should run after old cleanup"
        assert seen == [("replay", 11), ("camera", 12)]
    finally:
        cleanup.set()
        worker.stop.set()
        thread.join(3)


if __name__ == "__main__":
    published = []
    original = worker_mod.publish

    def fake_publish(client, topic, body, qos=0):
        published.append((topic, body))
        return len(str(body))

    worker_mod.publish = fake_publish
    try:
        test_command_matrix()
        print("PASS test_command_matrix")
        test_publish_and_state(published)
        print("PASS test_publish_and_state")
        test_lease_expiry_stops(published)
        print("PASS test_lease_expiry_stops")
        test_lease_renewal_keeps_running(published)
        print("PASS test_lease_renewal_keeps_running")
        test_cloud_retains_newer_start_during_previous_cleanup(published)
        print("PASS test_cloud_retains_newer_start_during_previous_cleanup")
    finally:
        worker_mod.publish = original
