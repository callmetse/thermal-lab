# System verification and remaining checks

Scope: what was executed locally on 2026-11-20 against the tree in this branch, what the
result was, and which parts of the system remain unverified. The tests prove the imported
source is internally consistent and runs on a workstation. They do not prove that any
device runs this revision.

## Local test evidence

Python 3.13.16 on Windows. Each suite used temporary directories and loopback HTTP; no
device, broker, GPU or production database was contacted.

| Suite | Command | Result |
|---|---|---|
| Environment gateway | `python -m unittest discover -s environment/tests -v` | Ran 35 tests, OK |
| Environment long-term (revision) | `python -m unittest discover -s revisions/environment-longterm/tests -v` | Ran 36 tests, OK |
| Audio | `cd acoustic && python -m unittest discover -s tests -v` | Ran 67 tests, OK (skipped 5) |
| Vision | `cp vision/config.example.json vision/config.json && cd vision && python -m unittest discover -s tests -v` | Ran 122 tests, OK |
| Collaboration overlay (revision) | `cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests -v` | Ran 71 tests, OK |

331 tests ran, 5 were skipped, none failed.

The same five suites were re-run on Ubuntu 24.04 with Python 3.12.3 (`aiohttp` 3.14.5, `numpy`
1.26.4) from the same commit: identical counts, 0 failures. `.github/workflows/tests.yml` runs
them on Python 3.12 and 3.13 for every push and pull request.

Notes on the run:

- Vision requires a local `config.json`; `config.example.json` is the documented template and
  `config.json` is ignored. Without it the contract tests fail on the missing file, not on
  behaviour.
- The 5 skipped audio tests require Sherpa-ONNX and its models, which are provisioned on the
  GPU host only.
- `revisions/collaboration` is a patch bundle, not a standalone package: its tests import
  `lab.*` from the base vision tree. Run without `PYTHONPATH=../../vision`, six test modules
  fail at import. The 71 tests above are the suite with the base package overlaid.

## Source identity: four files named `worker.py`

The file name does not identify a subsystem or a release. These are the four copies in the
tree, with their SHA-256 digests:

| Path | Subsystem | Lines | SHA-256 (first 16) | Relation |
|---|---|---|---|---|
| `vision/lab/worker.py` | Vision, base tree | 805 | `fe10e1e8d14b1c5f` | Edge and cloud roles selected by argument |
| `revisions/vision-extension/lab/worker.py` | Vision, extended archive snapshot | 1074 | `4b277770d4b58f13` | 317 differing lines against the base worker; adds dataset APIs, collection and monitoring paths |
| `revisions/collaboration/worker.py` | Vision, collaboration bundle | 761 | `026d06a8824b44e9` | 56 differing lines against the base worker; collaboration-mode variant, tested only with the base `lab` package |
| `acoustic/worker.py` | Audio, cloud worker | 425 | `c4bf1ab017a0d572` | Unrelated subsystem; Sherpa-ONNX endpoint detection |

Digests are taken from the committed blob (LF line endings), which is what GitHub serves, not from a
CRLF working copy. Records produced earlier against a Windows checkout carried different digests for
the same files; use the values above when comparing with a download.

The base tree (`vision/lab`) and the extended snapshot (`revisions/vision-extension/lab`) differ
in 26 entries: 12 files are modified and 14 exist in one tree only. Neither is asserted to be the
revision running on the Pi or the GPU host.

## Deployed source on the Raspberry Pi (2026-11-20)

Read-only comparison against the live Pi at `192.168.5.8` (`/home/pi/abc20261102`,
`/home/pi/mm_audio_v1`, `/home/pi/env_sensor_v1`). The GPU relay refused connections on port 33009
and the control host was not reached, so the cloud tiers remain uncompared.

- 89 files exist under both a repository path and a Pi path: 11 are byte-identical, 52 differ only
  in line endings (LF on the Pi, CRLF in a Windows working copy), and **26 differ in content**.
- 88 repository files have no Pi counterpart: tests, standalone tools, manifests and documentation
  are not deployed to the device.
- 59 Pi files are absent from the repository; about 37 of them are source or configuration,
  including `mm_audio_v1/setup_capture.sh` and `mm_audio_v1/gateway.sh`.
- The Pi vision tree is a hybrid build: `lab/collection_batches.py`, `lab/local_preview.py` and
  `lab/video_uploads.py` match `revisions/vision-extension/lab/` exactly, while `lab/worker.py`
  and `lab/common.py` match none of the copies in this tree. The deployed vision Supervisor
  configuration also runs a `dataset-api` program that has no counterpart under `vision/deploy/`.

This repository is therefore a local source snapshot, not the deployed revision. The captured
device tree is archived under [`deployments/pi-20261120/`](../deployments/pi-20261120/) and the
full comparison, including every differing file, is in
[`pi-diff-20261120.md`](pi-diff-20261120.md). Capturing the cloud tiers is still outstanding.

## Cloud tiers (captured 2026-11-20)

- **GPU** was reached read-only at `root@connect.nmb2.seetacloud.com:31380` (container
  `db94499d91`, RTX 3090): `/root/abc20261102` with five Supervisor programs RUNNING
  (`vision-cloud`, `llm-adapter`, `ollama`, `mqtt-tunnel`, `media-broker`). Its source is archived
  under `deployments/gpu-20261120/` (41 files).
- **Control host** was reached read-only; its Node-RED deployment, units, console files and code
  are archived under `deployments/control-20261120/` (69 files). The deployed `flows-native.json`
  (2026-11-17) is newer than the copy in this repository.
- **Four-way vision comparison** — the repository, the Pi, the GPU and the control host — is in
  [deployments-20261120.md](deployments-20261120.md). The Pi and GPU run the same extended build
  apart from `worker.py` and `llm.py`; the control host runs an older base build with its own
  edits; this repository holds the base build, and `revisions/vision-extension/` matches the Pi and
  GPU for 18 of the 28 files (the exception is `worker.py`).
- Five distinct `worker.py` builds have been observed: the repository copy, the Pi, the GPU, the
  control host, and the 2026-11-16 GPU baseline (`c40d2040`). The file name identifies none.
- The 2026-11-16 baseline (`05_实测结果/baseline_20261116_1643/`) remains the only record of the
  previous GPU instance, which was unreachable on every recorded port until 2026-11-20 21:46.
- The GPU SSH endpoint is **not stable**: AutoDL assigns a new port (and sometimes a different
  relay host) on every boot — 12434 → 23860 → 33009 → 31380 across these records. Treat the
  endpoint in any snapshot as timestamped evidence; see [gpu-endpoint.md](gpu-endpoint.md).

## Audio launch configuration gaps

The archived Supervisor template (`acoustic/deploy/supervisord.mm_audio_v1.ini`) starts three
programs. Two of the commands do not resolve to a file in this tree:

| Item | State | Consequence |
|---|---|---|
| `gateway.sh` | Generated by `acoustic/deploy.py` at deploy time; the generated file exists on the Pi (191 bytes, read 2026-11-20). It sources `/home/pi/.config/abc20261102/console-auth.env` and execs Caddy from `/home/pi/abc20261102/runtime/viewer/caddy`, both outside this repository. | The `audio-gateway` program cannot be reproduced from this tree alone; it depends on a generated file and on the control-plane runtime. |
| `setup_capture.sh` | **Found on the Pi** at `/home/pi/mm_audio_v1/setup_capture.sh` (read 2026-11-20): it resolves the USB card through `audio_device.py`, disables AGC, applies a fixed capture gain and verifies the level. Absent from every inventoried source tree, so this repository still cannot reproduce it. | The capture-profile step is recoverable only from the device; the copy has not been imported yet. |
| `utt_listener.py` | Source exists at `acoustic/tools/utt_listener.py`. `acoustic/deploy/start_listener.sh` launches `utt_listener.py` from `/home/pi/mm_audio_v1`, the service root. | Deployment must copy the file to the service root or change the launch path; the source layout is not the launch layout. |

The archived template matches the Pi: the deployed `/home/pi/mm_audio_v1/supervisord.ini` runs
`audio-edge`, `audio-gateway` and `audio-profile`, as the template does. `acoustic/deploy.py` writes
only the first two, so the generator output is the older variant and the archive reflects the device.

## Included and withheld files

- `vision/tools/deploy_ui_d.py` is not included. The original contained a plaintext console
  login password and a path into the operator's local secret store, and it rewrites the host
  UI-login JSON. Shipping it would have required either the credential or an unverified
  substitution.
- Other provisioning scripts are included with example hosts and ports. They still assume
  host-specific paths and identities; review them before use.
- `vision/tools/deploy_lan_console.py` carries one sanitization correction: the PowerShell
  directory path for the local secret store is restored to a directory (`thermal-lab/private`),
  because the automated substitution had produced a file-shaped path.
- Weights, recordings, SQLite databases, compiled firmware and local configuration are excluded
  by `.gitignore`. Model checksums are recorded in `vision/MODEL_MANIFEST.json` and
  `acoustic/MODEL_MANIFEST.json`.

## Evidence status

| Claim | Status |
|---|---|
| Source trees import and their test suites pass locally | Verified on 2026-11-20, as above |
| Repository contains no credential found by pattern scan | Verified with gitleaks 8.30.1 on the working tree and the commit; no leaks reported |
| Source equals deployed revision | **False for the Pi**: 26 files differ in content, the deployed vision tree is a hybrid, and the Pi carries scripts absent from the tree (2026-11-20). GPU and control hosts unverified. |
| Deployed revision equals running process | Unverified |
| Pi audio capture, ESP32 firmware build, Node-RED flow import, GPU cloud inference | Pi read-only inspected on 2026-11-20 (files and Supervisor programs); capture, firmware build, flow import and cloud runtime still unverified |

## Remaining checks before deployment

1. Capture the live Pi tree (source, Supervisor configurations and deploy-time scripts) as a
   labelled snapshot, then reconcile the 26 content differences before applying any overlay.
2. Import `setup_capture.sh` and `gateway.sh` from the Pi after sanitising them, and confirm the
   `audio-profile` program stays enabled on the device.
3. Identify the revision deployed on the GPU host and the control host, and compare it with this
   tree; the GPU relay was unreachable on 2026-11-20.
4. Install model weights and confirm checksums against both manifests.
5. Build and flash the ESP32 firmware, then verify sensor events reach the gateway over HTTP.
6. Import the Node-RED flows and verify session commands and results over MQTT.
7. Run one end-to-end experiment on the hardware and record the revision of every component in
   the result metadata.
