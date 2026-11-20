# Runtime verification: do the devices run the bytes we captured?

Read-only check on 2026-11-20 22:16 JST, answering one question: **is the code a service is running
the same code that sits on its disk and in the archive?**

No device was modified and no service was restarted.

## Method

1. For each long-running service, read the PID and its start time from `/proc/<pid>`, its working
   directory from `/proc/<pid>/cwd`, and its command line.
2. Hash every module of the package it imports, on the device, and compare with the hashes taken at
   capture time (Pi) or with the archive copy (GPU, after normalising line endings).
3. Compare the **newest write time of the package** with the **process start time**. If every module
   was last written before the process started, the interpreter had no newer bytes to load, and
   nothing has been rewritten since.
4. For Node-RED, read the flow file path from the unit environment, then compare the file's mtime
   with the console process start time.

## Results

| Tier | Service (PID) | Start | Entry point / cwd | Newest package write | Gap | Verdict |
|---|---|---|---|---|---|---|
| Pi | vision-edge (1554) | 2026-11-20 21:33:35 | `python -m lab.worker edge`, cwd `/home/pi/abc20261102` | 2026-11-18 10:55:05 | 58.6 h | running = disk |
| Pi | dataset-api (1553) | 2026-11-20 21:33:35 | `python -m lab.dataset_api`, same cwd | 2026-11-18 10:55:05 | 58.6 h | running = disk |
| Pi | env gateway (1965) | 2026-11-20 21:33:49 | `env_gateway.py`, cwd `/home/pi/env_sensor_v1` | 2026-11-17 20:33:34 | 73.0 h | running = disk |
| Pi | audio edge (2002) | 2026-11-20 21:34:16 | `edge.py`, cwd `/home/pi/mm_audio_v1` | 2026-11-10 22:22:12 | 239 h | running = disk |
| GPU | vision-cloud (1660) | 2026-11-20 21:13:40 | `python -m lab.worker cloud`, cwd `/root/abc20261102` | 2026-11-18 17:03:42 | 52.2 h | running = disk |
| GPU | llm-adapter (1661) | 2026-11-20 21:13:40 | `python -m lab.llm`, same cwd | 2026-11-18 17:03:42 | 52.2 h | running = disk |
| Control | Node-RED console (248654) | 2026-11-18 10:41:56 | `ABC_FLOW_FILE=/srv/abc20261102/deploy/nodered/flows-native.json` | 2026-11-17 20:31:55 | 14.2 h | running = disk |

Supporting checks:

- **The Pi's files have not changed since capture.** All 50 source files were re-hashed at 22:16; every
  digest is present in the capture-time list taken at 20:29.
- **The archive matches the device on disk.** For the Pi and the GPU lab packages: 23 of 28 files are
  byte-identical after newline normalisation; the other 5 (`common.py`, `object_line.py`,
  `presentation.py`, `scene_gate.py`, `vision.py`) differ only because the archive replaced host
  addresses and account names. The same five files on both tiers, as expected.
- Every process runs from the component directory that contains the tree it imports; there is no
  second copy on the search path.

## What this proves, and what it does not

**Proved:** the services listed above are executing the same bytes that are on the device now and in
`deployments/` — the chain *archive = disk = loaded image* holds for them, because no file was
written after the process started.

**Not proved:**

- Behaviour: a process can start, load the right bytes, and still be exercising a different code path
  (env vars, model files, runtime data). This check verifies provenance, not correctness.
- In-process reload: Python could in principle re-import a module at run time. Nobody instrumented the
  processes; the code contains no reload hook, and no file was written after start, so a reload would
  still have loaded the same bytes.
- Services that are not long-running (cron jobs, one-shot deploy scripts, the ESP32 firmware) were not
  covered.
- The repository now matches the **GPU** build; the Pi lags by two files and the control host runs an
  older base build — see [deployments-20261120.md](deployments-20261120.md). What is verified here is
  that each tier runs its own captured snapshot, not that all tiers run one identical tree.

## Reproducing

```bash
# on each host: PID, start time and working directory
for p in <pids>; do stat -c %Y /proc/$p; readlink /proc/$p/cwd; done
# newest write in the package
find <package> -name '*.py' -printf '%T@ %p\n' | sort -n | tail -1
# hashes, normalising CRLF before comparing with the archive
sha256sum <package>/*.py
```

Compare the newest write time with the process start time; if it is earlier, the running process was
started after the last edit and therefore loaded those bytes.
