# Thermal Lab — Edge–Cloud Sensing and Control Platform

An indoor thermal-comfort research platform: an ESP32 sensor node, a Raspberry Pi edge tier
(environment gateway, vision, audio), a GPU cloud tier for inference, and a Node-RED control
console on the control host.

## ⚠️ Project Status

This repository is a **review snapshot of the source**, not a released system. The local test
suites pass, but no device has been verified to run this revision. Treat deployment scripts as
historical material and re-check every host path before use.

## 🚀 Overview

The platform collects environmental, visual and acoustic evidence from a laboratory room and
feeds it to experiments on occupancy, comfort and HVAC control. The four roles are:

- **ESP32 node `env01`** — SHT4x (temperature/humidity) and SCD41 (CO₂) measurements, published
  to the Pi over HTTP with sequence numbers, clock state and heartbeats.
- **Raspberry Pi edge** — validates and stores sensor events (SQLite), runs edge vision and audio
  capture, and forwards session media to the cloud.
- **GPU cloud** — vision inference (pose, action) and audio inference (ASR, sound tagging).
- **Control host** — MQTT broker, Node-RED flows, Caddy console, session orchestration.

Environmental data uses HTTP; vision and audio use MQTT.

## 🛠️ Technology Stack

### Core

- **Python 3.12+** (3.13 verified locally), `aiohttp` HTTP gateway, `sqlite3` storage
- **MQTT** (paho) for vision/audio transport, **Node-RED** for control flows
- **OpenCV / Ultralytics YOLO** for vision, **Sherpa-ONNX** for audio ASR
- **ESPHome** firmware for the ESP32-S3 node
- **systemd / Supervisor** for services, **Caddy** for the LAN console, Docker Compose for the
  GPU and control hosts

### Development

- `unittest` suites (331 tests), `gitleaks` for credential scanning, GitHub Actions for CI

## 📁 Project Structure

```
thermal-lab/
├── environment/                 # Environmental sensing
│   ├── env_gateway.py           # HTTP gateway: validation, event id, replay protection
│   ├── env_schema.py            # Event contract (clock, sequence, ranges, quality)
│   ├── env_store.py             # SQLite store: latest/history, pruning, staleness
│   ├── supervisord.ini          # Pi launch template
│   ├── run_env_gateway.sh       # Pi start script
│   ├── firmware/                # ESPHome definition and HTTP publisher
│   └── tests/                   # 35 tests
├── vision/                      # Edge + cloud vision
│   ├── lab/                     # worker, camera, preview, media transport, archive
│   ├── deploy/                  # systemd units, Node-RED flows, compose files, Caddy
│   ├── tools/                   # experiment runners, exporters, provisioning helpers
│   ├── vendor/algorithm/        # temporal action model
│   ├── config.example.json      # configuration template (copy to config.json)
│   └── tests/                   # 122 tests
├── acoustic/                    # Audio capture and inference
│   ├── edge.py                  # Pi capture, session control
│   ├── worker.py                # Cloud ASR worker (Sherpa-ONNX)
│   ├── common.py, merge.py      # packets, transcripts, semantic merge
│   ├── deploy/                  # Supervisor template, listener launcher
│   └── tests/                   # 67 tests (5 need GPU-host models)
├── revisions/                   # Archived snapshots, kept separate on purpose
│   ├── vision-extension/        # later partial vision snapshot (dataset APIs, monitoring)
│   ├── environment-longterm/    # alternative gateway with long-term storage
│   └── collaboration/           # collaboration bundle (overlay on the base vision tree)
├── deployments/                 # Read-only archive of what a device actually ran
│   ├── pi-20261120/             # Pi source capture, 2026-11-20 (sanitised, with SHA256SUMS)
│   └── control-20261120/        # Control-host Node-RED flows, units and console files
├── docs/
│   ├── system-verification.md   # test evidence, worker identities, gaps
│   ├── pi-diff-20261120.md      # full comparison between this tree and the Pi
│   ├── frontend-20261120.md     # experiment bench: routes and deployed front-end files
│   ├── environment-gateway.md   # gateway behaviour and worked example
│   └── README.md                # review entry point per role
├── .github/workflows/tests.yml  # CI: five suites on Linux
├── requirements-dev.txt         # dependencies for the test suites
├── README.md · README.zh-CN.md  # English and Chinese entry points
├── CONTRIBUTING.md · LICENSE
```

This repository is a **multi-component source tree**, not a single pip-installable package. Each
component carries its own requirements and tests.

## 🔧 Installation

### Prerequisites

- Python 3.12+ (3.13 tested)
- Linux for deployment; the tests run on macOS, Linux and Windows

### Clone and install test dependencies

```bash
git clone https://github.com/callmetse/thermal-lab.git
cd thermal-lab
python -m pip install -r requirements-dev.txt
```

### Component dependencies

```bash
# Environment gateway (aiohttp)
python -m pip install -r environment/requirements.txt

# Vision (heavy: OpenCV, Ultralytics, torch; CPU-only machines can run the tests)
python -m pip install -r vision/requirements-runtime.txt

# Audio (Sherpa-ONNX and models are provisioned on the GPU host only)
python -m pip install -r acoustic/requirements-runtime.txt
```

Firmware: compile `environment/firmware/env01.yaml` with ESPHome after copying
`secrets.yaml.example` to `secrets.yaml` and filling in Wi-Fi, OTA and gateway values.

## 🧪 Running Tests

```bash
# Environment gateway
python -m unittest discover -s environment/tests -v

# Long-term gateway revision
python -m unittest discover -s revisions/environment-longterm/tests -v

# Audio
cd acoustic && python -m unittest discover -s tests -v && cd ..

# Vision (copy the template first; config.json is gitignored)
cp vision/config.example.json vision/config.json
cd vision && python -m unittest discover -s tests -v && cd ..

# Collaboration bundle (not standalone: overlays the base vision package)
cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests -v
```

Expected: **331 tests ran, 5 skipped, 0 failures**. The five skipped audio tests need Sherpa-ONNX
models that live on the GPU host. CI runs the same five suites on Ubuntu.

## 🏗️ Architecture

```
ESP32 env01 ──HTTP──▶ Pi env gateway ──SQLite──▶ query page
                              │
Pi camera ──▶ vision-edge ──MQTT preview/media──▶ vision-cloud (GPU)
                              │
Pi USB mic ─▶ audio-edge ──MQTT packets────────▶ audio-cloud (ASR) ──▶ events
                              │
                    control host: Node-RED flows ◀──MQTT──▶ console (Caddy)
```

See [docs/environment-gateway.md](docs/environment-gateway.md) for the gateway contract and
[docs/system-verification.md](docs/system-verification.md) for evidence status and known gaps.

## 📝 Configuration

- Copy `vision/config.example.json` to `vision/config.json` and adjust devices, MQTT, media and
  model settings. `config.json` is gitignored.
- Copy `environment/firmware/secrets.yaml.example` to `secrets.yaml` for Wi-Fi, OTA and gateway.
- Host addresses in this tree are examples (`192.0.2.x`, `gpu.example.invalid`); replace them with
  site values locally. Provisioning scripts read `THERMAL_SSH_DIR` and `THERMAL_LAB_PASSWORD` from
  the environment.

## 📦 Data and Models

Model weights, recordings, databases and local configuration are **excluded** from the tree. The
checksums of the weights used by this revision are recorded in `vision/MODEL_MANIFEST.json` and
`acoustic/MODEL_MANIFEST.json`; provision approved files separately and verify them against those
manifests.

## ⚠️ Known Gaps and Open Issues

| Gap | State | Effect |
|---|---|---|
| `setup_capture.sh` | present only on the Pi (found 2026-11-20); absent from every inventoried source tree | the capture-profile step cannot be reproduced from the repository; recover the copy from the device |
| `gateway.sh` | generated by `acoustic/deploy.py`; the generated file exists on the Pi; depends on `/home/pi/.config/abc20261102/console-auth.env` and the control-host Caddy runtime | the audio gateway cannot be reproduced from this tree alone |
| `utt_listener.py` launch path | source lives in `acoustic/tools/`; `deploy/start_listener.sh` starts it from the service root | deployment must copy or repoint it |
| Supervisor template vs generator | the archived template matches the Pi; `deploy.py` writes a shorter configuration without `audio-profile` | the generator is the older variant; do not regenerate over a deployed host |
| `vision/tools/deploy_ui_d.py` | withheld: contains a plaintext console password and a local secret-store path | excluded rather than shipping a working credential |
| Deployed vision differs from this tree | the Pi runs extended modules plus a `dataset-api` program; 26 files differ in content | see `docs/system-verification.md` before treating `vision/` as the deployed source |
| Front-end bench not current | repository `page.html` is a 2026-11-16 local edit (52 KB) while both devices serve the 2026-11-03 page (35 KB); the control host's `flows-native.json` (2026-11-17, 102 KB) is newer than the repository copy | neither tree is authoritative for every file; see `docs/frontend-20261120.md` |

## 🔍 Evidence Status

| Claim | Status |
|---|---|
| Source imports; five suites pass locally and in CI | ✅ 2026-11-20, Python 3.13 (Windows) and 3.12 (Ubuntu) |
| No credentials in the tree or history | ✅ gitleaks 8.30.1, no leaks |
| Source equals the deployed revision | ❌ false for the Pi: 26 files differ in content, deployed vision is a hybrid build (2026-11-20); GPU and control hosts unverified |
| Deployed revision equals the running process | ⬜ unverified |
| Pi capture, ESP32 firmware, Node-RED import, GPU runtime | ⬜ unverified; the Pi was read-only inspected on 2026-11-20 |

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: one branch per change, keep the suites green,
and record new deployment facts in `docs/system-verification.md` rather than in chat.

## 📄 License

BSD-2-Clause. See [LICENSE](LICENSE).

## 📬 Contact

Maintainer: [@callmetse](https://github.com/callmetse).

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。
