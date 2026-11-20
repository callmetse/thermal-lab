"""Collect a read-only monitoring baseline and verify an isolated SQLite restore."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time
from urllib.request import Request

# Avoid implicit proxies or redirects for credentials and local service checks.
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())
PREVIEW = "http://127.0.0.1:28990/health"
DATASET = "http://127.0.0.1:28992/health"
ENVIRONMENT = "http://127.0.0.1:28984/env/v1/latest?zone_id=lab"


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(url, authorization=None):
    started = time.monotonic()
    try:
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        with OPENER.open(Request(url, headers=headers), timeout=3) as response:
            payload = json.loads(response.read(256 * 1024))
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
        return payload, {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except (OSError, ValueError) as error:
        return {}, {"ok": False, "error": type(error).__name__, "http_status": getattr(error, "code", None)}


def sensor_state(payload):
    fields = payload.get("fields") or {}
    thresholds = payload.get("stale_after_ms") or {}
    state = {}
    for key, sensor in (("temperature_c", "sht4x"), ("humidity_rh", "sht4x"), ("co2_ppm", "scd41")):
        field = fields.get(key) or {}
        age, value, cutoff = field.get("age_ms"), field.get("value"), thresholds.get(key)
        fresh = (field.get("present") is True and field.get("stale") is False
                 and field.get("quality") == "ok" and field.get("sensor_id") == sensor
                 and type(age) in (float, int) and type(cutoff) in (float, int) and 0 <= age <= cutoff
                 and type(value) in (int, float) and math.isfinite(value))
        state[key] = {"sensor_id": sensor, "fresh": fresh, "age_ms": age, "limit_ms": cutoff}
    return state


def pi_host(root):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    temp = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=2)
        flags = int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        flags = None
    processes = {}
    supervisor = root / ".venv/bin/supervisorctl"
    try:
        status = subprocess.run([str(supervisor), "-c", str(root / "deploy/supervisord.ini"), "status"],
                                capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        status = ""
    for line in status.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1:3] != ["RUNNING", "pid"]:
            continue
        pid = int(parts[3].rstrip(","))
        try:
            proc = Path("/proc") / str(pid)
            rss = next(line.split()[1] for line in (proc / "status").read_text().splitlines() if line.startswith("VmRSS:"))
            tick = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            processes[parts[0]] = {"pid": pid, "rss_bytes": int(rss) * 1024,
                                   "start_s_since_boot": int(tick) / os.sysconf("SC_CLK_TCK")}
        except (OSError, IndexError, StopIteration):
            continue
    return {"hostname": socket.gethostname(), "boot_id": boot, "processes": processes,
            "disk_free_bytes": shutil.disk_usage(root).free, "tmpfs_free_bytes": shutil.disk_usage("/dev/shm").free,
            "temperature_c": int(temp.read_text()) / 1000 if temp.exists() else None,
            "throttle_flags": flags, "current_undervoltage": bool(flags & 1) if flags is not None else None,
            "latched_undervoltage": bool(flags & (1 << 16)) if flags is not None else None}


def listener_connection(port):
    # This measures TCP listener reachability. End-to-end MQTT delivery needs a workload.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def pi_sample(root):
    preview, preview_probe = read(PREVIEW)
    dataset, dataset_probe = read(DATASET)
    env, env_probe = read(ENVIRONMENT)
    preview_probe["ok"] &= preview.get("transport") == "pi-local"
    dataset_probe["ok"] &= dataset.get("archive_available") is True
    sensors = sensor_state(env)
    env_probe["ok"] &= env.get("zone_id") == "lab" and all(v["fresh"] for v in sensors.values())
    return {"utc": utc(), "pi": pi_host(root),
            "probes": {"preview": preview_probe, "dataset": dataset_probe, "environment": env_probe},
            "sensors": sensors, "archive_ingest": preview.get("archive_ingest"),
            "preview_run_id": preview.get("run_id"), "dataset_run_id": dataset.get("active_run_id"),
            "dataset_generation": dataset.get("session_generation"),
            "archive_error": bool(preview.get("archive_error") or dataset.get("batch_capture_error")),
            "local_tcp": {"control_mqtt": listener_connection(28883), "media_mqtt": listener_connection(28583)}}


def reconciliation(sample, control):
    """Report conflicting evidence for an operator. No session mutation."""
    if not sample.get("probes", {}).get("dataset", {}).get("ok"):
        return "dataset_unavailable"
    if not sample.get("probes", {}).get("preview", {}).get("ok"):
        return "worker_unavailable"
    if "active" not in control or not isinstance(control.get("session"), dict):
        return "control_unavailable"
    active = control["active"]
    observed = sample["preview_run_id"], sample["dataset_run_id"]
    if active is None:
        return "awaiting_reconciliation:control_idle_with_pi_run" if any(observed) else "idle"
    if not isinstance(active, dict):
        return "control_unavailable"
    age = control["session"].get("heartbeat_age_s")
    ttl = control["session"].get("lease_ttl_s")
    if (type(age) not in (float, int) or type(ttl) not in (float, int) or ttl <= 0
            or not 0 <= age <= min(10, ttl)):
        return "awaiting_reconciliation:worker_heartbeat_missing_or_stale"
    run_id = active.get("run_id")
    if not run_id or observed != (run_id, run_id):
        return "awaiting_reconciliation:run_ids_disagree"
    if (type(active.get("generation")) is not int
            or active["generation"] != sample["dataset_generation"]):
        return "awaiting_reconciliation:generation_disagrees"
    return "running_aligned"


def metrics(sample):
    lines = [f'abc_ops_last_sample_timestamp_seconds {datetime.fromisoformat(sample["utc"]).timestamp()}']
    for name, probe in sample["probes"].items():
        lines.append(f'abc_ops_probe_success{{probe="{name}"}} {int(probe["ok"])}')
    for name, value in sample["sensors"].items():
        lines.append(f'abc_ops_sensor_fresh{{field="{name}"}} {int(value["fresh"])}')
        if type(value["age_ms"]) in (int, float):
            lines.append(f'abc_ops_sensor_age_seconds{{field="{name}"}} {value["age_ms"] / 1000}')
    for key in ("disk_free_bytes", "tmpfs_free_bytes", "temperature_c", "throttle_flags",
                "current_undervoltage", "latched_undervoltage"):
        value = sample["pi"][key]
        if value is not None:
            name = "temperature_celsius" if key == "temperature_c" else key
            lines.append(f"abc_ops_pi_{name} {int(value) if type(value) is bool else value}")
    archive = sample.get("archive_ingest") or {}
    for key in ("submitted", "delivered", "dropped", "retries", "backlog", "oldest_age_s", "flush_timeouts"):
        value = archive.get(key)
        if type(value) in (int, float):
            lines.append(f"abc_ops_archive_{key} {value}")
    return "\n".join(lines) + "\n"


def write_metrics(path, sample):
    if path.suffix != ".prom":
        raise ValueError("Use a .prom destination in the node_exporter textfile directory")
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        tmp = Path(temp.name)
        temp.write(metrics(sample).encode())
    try:
        tmp.chmod(0o644)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def backup_restore(source, workdir, app_root=None, batch_id=None):
    """Take a SQLite online backup, restore separately, compare stable snapshot totals."""
    with TemporaryDirectory(dir=workdir) as tmp:
        snapshot, restored = (Path(tmp) / name for name in ("snapshot.sqlite3", "restored.sqlite3"))
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as live:
            with closing(sqlite3.connect(snapshot)) as backup:
                backup_deadline = time.monotonic() + 20

                def check_deadline(status, remaining, total):
                    if time.monotonic() > backup_deadline:
                        raise TimeoutError("Backup exceeded 20 seconds")

                live.backup(backup, pages=256, progress=check_deadline, sleep=0.05)
        shutil.copyfile(snapshot, restored)

        def totals(path):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("SQLite integrity check failed")
                return db.execute("""
                    SELECT b.batch_id, COALESCE(o.total, 0), COALESCE(e.total, 0) FROM batches AS b
                    LEFT JOIN (SELECT batch_id, COUNT(DISTINCT key) AS total FROM experiment_records
                               WHERE kind='observation' GROUP BY batch_id) AS o USING(batch_id)
                    LEFT JOIN (SELECT batch_id, COUNT(DISTINCT event_id) AS total FROM batch_environment
                               GROUP BY batch_id) AS e USING(batch_id)
                    ORDER BY b.batch_id
                """).fetchall()

        before, after = totals(snapshot), totals(restored)
        if before != after:
            raise ValueError("Restored batch or record totals differ")
        result = {"status": "PASS", "utc": utc(), "batches": len(after), "snapshot_bytes": snapshot.stat().st_size,
                  "unique_observations": sum(row[1] for row in after), "environment_rows": sum(row[2] for row in after)}
        if batch_id is not None:
            expected = next(row[1:] for row in after if row[0] == batch_id)
            result["export"] = restored_export(restored, app_root, batch_id, expected)
        return result


def restored_export(database, app_root, batch_id, expected):
    import sys
    import zipfile

    sys.path.insert(0, str(app_root.resolve()))
    from lab.collection_batches import CollectionBatches
    from lab.experiment_archive import ExperimentArchive

    store = CollectionBatches(database, database.parent / "environment.sqlite3", mark_interrupted=False)
    archive = ExperimentArchive(store, database.parent)
    try:
        output = archive.export(batch_id)
        try:
            with zipfile.ZipFile(output) as package:
                if package.testzip() is not None:
                    raise ValueError("ZIP integrity check failed")
                with package.open("observations.jsonl") as rows:
                    observations = sum(1 for row in rows if row.strip())
                with package.open("environment.jsonl") as rows:
                    environment = sum(1 for row in rows if row.strip())
                summary = json.loads(package.read("summary.json"))
                if (observations, environment) != expected or summary["observation_count"] != observations:
                    raise ValueError("Export counts disagree with the restored database")
                return {"batch_id": batch_id, "observations": observations, "environment_rows": environment,
                        "archive_status": summary["batch"].get("archive_status")}
        finally:
            shutil.rmtree(output.parent)
    finally:
        archive.close()


def monitor(args):
    from base64 import b64encode
    from urllib.parse import urlsplit

    origin = urlsplit(args.console_origin)
    if (origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise ValueError("Expected a trusted console origin without credentials or path")
    credential = os.environ.get("ABC_READ_CONSOLE_BASIC")
    if not credential:
        raise ValueError("Set ABC_READ_CONSOLE_BASIC from the existing console credential")
    auth = "Basic " + b64encode(credential.encode()).decode()
    source = Path(__file__).read_bytes()
    command = ["ssh", "-F", "NUL" if os.name == "nt" else "/dev/null",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
               "-i", str(args.identity), args.pi_target, "python3", "-", "pi"]
    when = started = time.monotonic()
    with args.timeline.open("x", encoding="utf-8", buffering=1) as timeline:
        while True:
            row = {"utc": utc(), "monitor_host": socket.gethostname(), "monitor_uptime_s": time.monotonic() - started,
                   "sample_delay_s": max(0, time.monotonic() - when)}
            try:
                proc = subprocess.run(command, input=source, capture_output=True, timeout=24, check=True)
                row["pi_sample"] = json.loads(proc.stdout)
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                row["pi_sample_error"] = type(error).__name__
            base = args.console_origin.rstrip("/")
            control, state = read(base + "/api/state", auth)
            batches, batch_probe = read(base + "/api/collection-batches?limit=1", auth)
            env, env_probe = read(base + "/api/environment/latest?zone_id=lab", auth)
            state["ok"] &= "active" in control and isinstance(control.get("session"), dict)
            batch_probe["ok"] &= isinstance(batches.get("batches"), list)
            env_probe["ok"] &= env.get("zone_id") == "lab" and all(v["fresh"] for v in sensor_state(env).values())
            row.update(console_probes={"state": state, "batches": batch_probe, "environment": env_probe},
                       reconciliation=reconciliation(row.get("pi_sample", {}), control))
            timeline.write(json.dumps(row, allow_nan=False) + "\n")
            if args.duration <= 0 or time.monotonic() - started >= args.duration:
                return
            when += args.interval
            if when < time.monotonic():
                # Keep a fixed cadence and record the skipped interval via elapsed times.
                when += math.ceil((time.monotonic() - when) / args.interval) * args.interval
            time.sleep(max(0, when - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pi = sub.add_parser("pi")
    pi.add_argument("--root", type=Path, default=Path("/home/pi/abc20261102"))
    pi.add_argument("--textfile", type=Path)
    backup = sub.add_parser("backup-restore")
    backup.add_argument("source", type=Path)
    backup.add_argument("--workdir", type=Path)
    backup.add_argument("--app-root", type=Path)
    backup.add_argument("--batch-id")
    watch = sub.add_parser("watch")
    watch.add_argument("--pi-target", required=True)
    watch.add_argument("--identity", required=True, type=Path)
    watch.add_argument("--console-origin", required=True)
    watch.add_argument("--timeline", required=True, type=Path)
    watch.add_argument("--interval", type=float, default=30)
    watch.add_argument("--duration", type=float, default=0, help="Seconds. Zero means one sample.")
    args = parser.parse_args()
    if args.command == "watch":
        if not math.isfinite(args.interval) or args.interval < 5 or not math.isfinite(args.duration) or args.duration < 0:
            parser.error("Expected a finite interval of at least five seconds and a nonnegative duration")
        monitor(args)
    elif args.command == "pi":
        result = pi_sample(args.root)
        if args.textfile:
            write_metrics(args.textfile, result)
        print(json.dumps(result, allow_nan=False))
    else:
        if bool(args.app_root) != bool(args.batch_id):
            parser.error("Supply --app-root and --batch-id together for a ZIP drill")
        print(json.dumps(backup_restore(args.source, args.workdir, args.app_root, args.batch_id), allow_nan=False))


if __name__ == "__main__":
    main()
