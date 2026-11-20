# Deployed snapshots

Archive copies of source that was running on a device, kept separate from the review tree.

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

**Do not edit this directory.** It is evidence of what the device ran; changes belong in
`environment/`, `vision/` or `acoustic/`, with the reconciliation described in the diff report.
