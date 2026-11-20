# Thermal Lab · Supervisor review

This branch (`review/full-system-source`) carries the full local source inventory: the
environment gateway, ESP32 firmware, vision and audio pipelines, and the archived revision
snapshots. Start with [system verification and remaining checks](system-verification.md),
which lists the executed test suites and the parts of the system that remain unverified.
The gateway-only pull request (#1) remains open for line-level comments on that module.

| Review item | Evidence |
|---|---|
| Executed local tests and evidence status | [System verification](system-verification.md) |
| Environment gateway behaviour | [Gateway guide](environment-gateway.md) |
| Line-level comments on the gateway | [PR #1: Files changed](https://github.com/callmetse/thermal-lab/pull/1/files) |
| Differences between the four `worker.py` copies | [Source identity](system-verification.md#source-identity-four-files-named-workerpy) |

## System coverage

| Role | Source in this branch | Evidence |
|---|---|---|
| Raspberry Pi: environment gateway | `environment/` — HTTP service, event schema, SQLite store, Pi launch template, ESP32 firmware | Local suite passes; Pi service configuration and a device-side run unverified |
| Raspberry Pi: vision | `vision/lab/` — edge worker, camera adapter, preview, media transport | Local suite passes; the revision deployed on the Pi is unverified |
| Raspberry Pi: audio | `acoustic/` — edge capture, wake word, dashboard, Supervisor template | Local suite passes (5 tests skipped without Sherpa-ONNX); the capture-profile script has no source, see verification notes |
| GPU cloud | `vision/lab/worker.py`, `acoustic/worker.py`, `vision/deploy/gpu.compose.yml` | Service definitions present; runtime, models and the deployed revision unverified |
| Control cloud | `vision/deploy/nodered/`, `control.compose.yml`, console gateway | Flow and proxy definitions present; import and live behaviour unverified |
| ESP32 node | `environment/firmware/env01.yaml`, `env_publish.h` | Firmware definition present; build, flash and sensor end-to-end unverified |
| Experiment revisions | `revisions/vision-extension/`, `revisions/environment-longterm/`, `revisions/collaboration/` | Preserved snapshots that differ from the base trees; the collaboration tests pass only with the base vision package overlaid |

Local tests establish that the imported source runs and is internally consistent. They do not
establish that any device runs this revision. An audit of repeated AI development attempts would
require task histories with attempt identifiers and token usage records, which the source tree
does not contain.
