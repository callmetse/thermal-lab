# Per-tier source trees

The complete source captured from each device, one directory per tier. These are the reference
copies: read them, compare against them, but do not edit them — the editable tree is at the
repository root (`vision/`, `acoustic/`, `environment/`), which carries the newest observed build.

| Tier | Directory | Files | Note |
|---|---|---|---|
| Raspberry Pi | `pi-20261120/` | 152 | includes the Pi's own `worker.py` and `llm.py` (10:55 build) |
| GPU | `gpu-20261120/` | 41 | matches the root trees byte for byte |
| Control host | `control-20261120/` | 69 | older base build plus its own edits |

## `pi-20261120/`

Captured read-only from the Raspberry Pi at `192.168.5.8` on 2026-11-20:

| Directory | Device path |
|---|---|
| `abc20261102/` | `/home/pi/abc20261102` — vision, tools, Node-RED deployment definitions |
| `mm_audio_v1/` | `/home/pi/mm_audio_v1` — audio capture, gateway script, capture profile |
| `env_sensor_v1/` | `/home/pi/env_sensor_v1` — environment gateway and its support scripts |

What the archive removes before committing:

- `config.json`, `hotspot-backup.nmconnection` and every file matched by the repository ignore
  rules (weights, databases, recordings, logs, PID/socket files, virtual environments).
- Host addresses and account names: `192.168.5.8` → `192.0.2.11`, `8.137.51.197` → `192.0.2.10`,
  `connect.nmb*.seetacloud.com` → `gpu.example.invalid`, the operator account name and key
  filenames replaced with neutral placeholders. Ports are kept because they are configuration
  facts, not secrets.
- Backup copies (`*.pre-*`, `*.bak-*`) and lock files.

`SHA256SUMS` lists the archived files as committed here, not the raw device files. The device
hashes measured at capture time are in [`../docs/pi-diff-20261120.md`](../docs/pi-diff-20261120.md).

Links inside a captured README may point at files that were not part of the capture (weights,
recordings, local notes); they are kept as they were on the device.

**Do not edit this directory.** It is evidence of what the device ran; changes belong in
`environment/`, `vision/` or `acoustic/`, with the reconciliation described in the diff report.

## `control-20261120/`

Captured read-only from the control host (the broker and Node-RED console) on 2026-11-20:

| Path in the archive | Control-host path |
|---|---|
| `deploy/nodered/` | `/srv/abc20261102/deploy/nodered` — Node-RED flows, page and settings |
| `deploy/` | systemd units, `console.Caddyfile`, compose files, bootstrap scripts |
| `lab/` | the vision package subset the control host runs (10 files) |
| `tests/` | the single contract test present on the host |
| `tools/` | 25 of the tool scripts present on the host |
| `research/` | the notes that were never imported into a source tree |
| `README.md`, `PROVENANCE.json`, `requirements-runtime.txt`, `package.json` | service root |

69 files in total. Excluded: `config.json`, `node_modules/`, `state/` runtime data (batches, logs,
Node-RED library), `package-lock.json`, and Node-RED's `.config.runtime.json` credential secret.
Hosts and account names use the same example substitutions as the Pi archive.

The front-end comparison between the repository, the control host and the Pi is in
[`../docs/frontend-20261120.md`](../docs/frontend-20261120.md).

## `gpu-20261120/`

Captured read-only from the GPU instance (`root@connect.nmb2.seetacloud.com:31380`, container
`db94499d91`) on 2026-11-20:

| Path in the archive | GPU path |
|---|---|
| `lab/` | `/root/abc20261102/lab` — extended vision package, 28 files |
| `deploy/` | Supervisor configuration, media broker config, tracking config |
| `vendor/` | tracking and temporal-action algorithm sources |
| `tools/` | the single native start script |
| `PROVENANCE.json` | build provenance recorded on the instance |

41 files. Excluded: `config.json`, `assets/` (weights and NCNN exports), `data/`, `state/`,
`results/`, `logs/`, `runtime/` and virtual environments. The same substitutions apply; the SSH
port was normalised to `22`.

The four-way comparison of the vision package across the repository, the Pi, the GPU and the
control host is in [`../docs/deployments-20261120.md`](../docs/deployments-20261120.md).
