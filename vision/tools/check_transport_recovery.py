"""Controlled SIGSTOP/SIGCONT of only the new GPU result tunnel; real camera experiment."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
import shlex
import time

from ssh_access import connection

ROOT = Path(__file__).resolve().parent.parent
GPU_ROOT, CONTROL_ROOT = "/root/abc20261102", "/srv/abc20261102"

# The detached, identity-checked rescuer survives SSH disconnect or caller termination.
# The primary remote process also restores in finally and handles HUP/TERM/INT.
RELEASE = """import os,signal,sys,time
from pathlib import Path
pid,identity,delay=int(sys.argv[1]),sys.argv[2],float(sys.argv[3])
time.sleep(delay)
try:
 if Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]==identity:os.kill(pid,signal.SIGCONT)
except ProcessLookupError:pass
except FileNotFoundError:pass
"""
FAULT = """import json,os,signal,subprocess,sys,time
from pathlib import Path
root='/root/abc20261102';pause=PAUSE
pid=int(subprocess.check_output([root+'/.venv/bin/supervisorctl','-c',root+'/deploy/supervisord.ini','pid','mqtt-tunnel']).strip())
if pid<=1:raise RuntimeError('No PID for new-project mqtt-tunnel')
cmd=Path('/proc/%d/cmdline'%pid).read_bytes()
if b'/root/.ssh/abc20261102_mqtt' not in cmd or b'root@192.0.2.10' not in cmd:raise RuntimeError('Refusing unrelated process')
identity=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]
subprocess.Popen([sys.executable,'-c',RELEASE, str(pid),identity,str(pause+2)],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def interrupted(signum,frame):raise SystemExit(128+signum)
for signum in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT):signal.signal(signum,interrupted)
begin=time.time()
try:
 os.kill(pid,signal.SIGSTOP);time.sleep(pause)
finally:
 try:
  if Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]==identity:os.kill(pid,signal.SIGCONT)
 except (ProcessLookupError,FileNotFoundError):pass
 print(json.dumps({'pid':pid,'pause_seconds':pause,'begin_wall_s':begin,'resumed_wall_s':time.time(),'detached_rescue_delay_s':pause+2}),flush=True)
"""


def checked(run, command, timeout=30):
    result = run(command, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Remote step failed: " + result.stderr.decode(errors="replace")[:180])
    return result.stdout


def collect(run, command, path):
    data = gzip.decompress(checked(run, command, timeout=45))
    path.write_bytes(data)
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def key(record):
    value = record.get("payload", {})
    if record.get("channel") != "observations" or value.get("observation_seq") is None or not value.get("producer_instance_id"):
        return None
    return value["run_id"], value["producer_instance_id"], value["observation_seq"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=40, help="Observed run time; server lease adds 15 seconds")
    parser.add_argument("--warmup-seconds", type=float, default=10)
    parser.add_argument("--pause-seconds", type=float, default=5)
    args = parser.parse_args()
    if not 0 < args.pause_seconds <= 10 or args.warmup_seconds < 0 or not args.warmup_seconds + args.pause_seconds + 3 < args.seconds <= 3585:
        parser.error("Need recovery time after fault; pause must be (0,10], lease <=3600s")
    (ROOT / "results").mkdir(exist_ok=True)
    keys = Path("/mnt/c/Users/operator/.ssh")
    samples, fault, fault_error, rid, triggered = [], None, None, None, None
    with connection("192.0.2.10", "root", 22, keys / "control-example", keys / "known_hosts") as control, connection("gpu.example.invalid", "root", 22, keys / "gpu-example", keys / "known_hosts") as gpu:
        def api(operation, body=None):
            command = "python3 " + CONTROL_ROOT + "/tools/control_api.py " + operation
            if body is not None:command += " " + shlex.quote(json.dumps(body))
            return json.loads(checked(control, command))
        if api("state").get("active"):
            raise RuntimeError("An experiment is active; refusing to replace it")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            try:
                run = api("start", {"arm":"C", "source":"camera", "seconds":args.seconds+15});rid = run["run_id"]
                start = time.monotonic();next_poll = start
                while time.monotonic() - start < args.seconds:
                    elapsed = time.monotonic() - start
                    if future is None and elapsed >= args.warmup_seconds:
                        triggered = elapsed
                        code = FAULT.replace("PAUSE", repr(args.pause_seconds)).replace("RELEASE", repr(RELEASE))
                        future = pool.submit(checked, gpu, GPU_ROOT + "/.venv/bin/python -c " + shlex.quote(code), 20)
                    state = api("state");active = state.get("active");obs = state.get("observation") or {}
                    samples.append({"elapsed_s":time.monotonic()-start,"active_run_id":active.get("run_id") if active else None,
                                    "producer_instance_id":obs.get("producer_instance_id"),"observation_seq":obs.get("observation_seq"),
                                    "source_frame_id":obs.get("source_frame_id"),"observation_age_s":state.get("observation_age_s"),"preview_id":state.get("preview_id")})
                    if not active or active.get("run_id") != rid:break
                    next_poll += 1;time.sleep(max(0, next_poll-time.monotonic()))
            finally:
                if future is not None:
                    try:fault = json.loads(future.result(timeout=22))
                    except Exception as error:fault_error = str(error)
                current = api("state").get("active")
                if current and current.get("run_id") == rid:api("stop", {})
        time.sleep(3)
        control_path = ROOT / "results/transport-recovery-control.jsonl"
        producer_path = ROOT / "results/transport-recovery-producer.jsonl"
        ccode = "import gzip,json,sys;from pathlib import Path;rid="+repr(rid)+";lines=[l.rstrip(bytes([10])) for l in Path('"+CONTROL_ROOT+"/data/events.jsonl').open('rb') if json.loads(l)['payload'].get('run_id')==rid];sys.stdout.buffer.write(gzip.compress(bytes([10]).join(lines)+bytes([10])))"
        pcode = "import gzip,sys;from pathlib import Path;p=Path('"+GPU_ROOT+"/data/results')/"+repr(rid)+";sys.stdout.buffer.write(gzip.compress(b''.join(f.read_bytes() for f in sorted(p.glob('*.jsonl')))))"
        received = collect(control, "python3 -c " + shlex.quote(ccode), control_path)
        produced = collect(gpu, GPU_ROOT + "/.venv/bin/python -c " + shlex.quote(pcode), producer_path)
    prod_keys = [key(r) for r in produced if key(r) is not None]
    recv_keys = [key(r) for r in received if key(r) is not None]
    prod, recv = Counter(prod_keys), Counter(recv_keys)
    last, reversed_keys = {}, []
    for item in recv_keys:
        stream = item[:2]
        if item[2] < last.get(stream, 0):reversed_keys.append(item)
        last[stream] = max(item[2], last.get(stream, 0))
    before = [s for s in samples if triggered is not None and s["elapsed_s"] <= triggered and s["source_frame_id"] is not None]
    after = [s for s in samples if triggered is not None and s["elapsed_s"] >= triggered+args.pause_seconds and s["active_run_id"] == rid]
    reference = before[-1] if before else None
    advanced = [s for s in after if reference and s["source_frame_id"] is not None and s["source_frame_id"] > reference["source_frame_id"]]
    report = {"run_id":rid,"controlled_fault":"SIGSTOP only new GPU mqtt-tunnel; control-routed input, results and commands all pause; workers remain running; capture continues on Pi",
              "observed_seconds":args.seconds,"lease_seconds":args.seconds+15,"fault_trigger_elapsed_s":triggered,"fault":fault,"fault_error":fault_error,
              "samples":samples,"produced_observations":len(prod_keys),"received_observations":len(recv_keys),
              "missing_at_control":sorted(prod.keys()-recv.keys()),"extra_at_control":sorted(recv.keys()-prod.keys()),
              "duplicate_producer_keys":[{"key":k,"count":v} for k,v in prod.items() if v>1],
              "duplicate_control_keys":[{"key":k,"count":v} for k,v in recv.items() if v>1],"reversed_control_keys":reversed_keys,
              "producer_keys_missing":sum(key(r) is None for r in produced),"producer_journal_found":bool(produced),
              "resumed_frame_advance":len({s["source_frame_id"] for s in advanced})>=2,"first_advanced_poll_after_expected_resume_s":advanced[0]["elapsed_s"]-triggered-args.pause_seconds if advanced else None,
              "resumed_preview_advance":bool(reference and any(s["preview_id"] and s["preview_id"]!=reference["preview_id"] for s in after)),
              "observation_age_basis":"Age since control received latest result, not capture-to-display AoI",
              "raw_control_jsonl":str(control_path),"raw_producer_jsonl":str(producer_path)}
    (ROOT / "results/transport-recovery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:report[k] for k in ["run_id","produced_observations","received_observations","producer_journal_found","resumed_frame_advance","resumed_preview_advance","fault_error"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
