# Thermal Lab: an edge–cloud platform for paired visual and environmental observation

Studies of indoor thermal comfort require observations in which the state of the occupied space and
the state of its occupants are recorded together and can be re-examined. This repository documents a
platform built for that purpose: a sensor node, an edge tier, a GPU inference tier and a control host,
together with the source trees recovered from the three deployed machines on 20 November 2026.

The repository is a source-review artefact. It holds code, deployment records and verification
evidence. Model weights, databases, recordings and credentials are not included, and every host,
account and port it publishes is an example value.

## 1. Status

The vision package in `vision/lab/` is the newest build observed in the deployment: `worker.py` and
`llm.py` are the copies written on the GPU instance on 2026-11-18 at 17:03, and the remaining 26
modules of that package are byte-identical on the Pi and the GPU.

| Claim | Evidence | Status |
|---|---|---|
| Seven long-running services execute the code recorded here | process start times postdate the last write of each package, 2026-11-20 22:16 JST | verified, `docs/runtime-verification.md` |
| The Pi source files are unchanged since capture | 50 files compared by digest | verified, `docs/runtime-verification.md` |
| Test suites pass | 330 cases, 12 skipped, 0 failures; CI on Python 3.12 and 3.13 | verified, Section 5.1 |
| The three tiers run one build | they do not: five `worker.py` builds and two `llm.py` builds are in use | divergence documented, Section 4 |
| The ESP32 node is reproducible from this tree | only its ESPHome definition is present; the image is neither built nor flashed here | open |

## 2. System

The platform acquires environmental, visual and acoustic evidence from a laboratory room and routes
it to experiments on occupancy, comfort and HVAC control. It has four tiers.

**Sensor node.** An ESP32-S3 board (`env01`) carries an SHT4x temperature and humidity sensor and an
SCD41 CO₂ sensor. It publishes measurements over HTTP with sequence numbers, clock state and
heartbeats, and is defined in `environment/firmware/env01.yaml`.

**Edge tier.** A Raspberry Pi validates and stores sensor events in SQLite, captures camera frames and
audio, runs edge detection, and forwards session media to the cloud. The environment gateway listens
on port 28984; the vision worker exposes a preview socket on 28990; the data plane runs on 28992; a
Caddy instance on 28880 is the single LAN entry for the browser console.

**GPU tier.** A rented GPU instance performs pose and action inference and audio inference (ASR,
sound tagging). It is reached through an SSH tunnel whose port is reallocated at each instance start.

**Control host.** A cloud host runs the MQTT broker, the Node-RED flows, the Caddy console and session
orchestration. The Pi reaches it through a second tunnel, exposed locally on 28881.

Environmental data travel over HTTP; vision and audio travel over MQTT. The bench exposes two browser
surfaces, `/lab` for experiment control and `/data` for retrieval, whose routing is documented in
[`docs/frontend-20261120.md`](docs/frontend-20261120.md).

## 3. Source tree

```
thermal-lab/
├── environment/                  # gateway, event contract, SQLite store, ESPHome firmware
│   ├── env_gateway.py            # HTTP entry, validation, replay protection
│   ├── env_schema.py             # event contract: clock, sequence, ranges, quality
│   ├── env_store.py              # storage, pruning, staleness
│   ├── env_longterm.py           # long-term interval records
│   ├── firmware/                 # env01.yaml, HTTP publisher header
│   └── tests/                    # 35 cases
├── vision/
│   ├── lab/                      # 28 modules: worker, camera, preview, transport, archive
│   ├── deploy/                   # systemd units, Node-RED flows, compose files, Caddy
│   ├── tools/                    # runners, exporters, provisioning helpers
│   ├── vendor/algorithm/         # temporal action model
│   ├── tests/                    # 121 cases
│   ├── config.example.json
│   └── MODEL_MANIFEST.json
├── acoustic/                     # capture, ASR, tagging; 67 cases
├── revisions/
│   ├── vision-base-20261120/     # earlier vision build, kept for comparison
│   ├── vision-extension/         # extension variant of the same package
│   ├── environment-longterm/     # long-term record revision; 36 cases
│   └── collaboration/            # collaboration overlay; 71 cases
├── sources/                      # recovered trees, 2026-11-20
│   ├── pi-20261120/              # 152 files, with SHA256SUMS
│   ├── gpu-20261120/             # 41 files, with SHA256SUMS
│   └── control-20261120/         # 69 files, with SHA256SUMS
├── docs/                         # evidence, architecture, deployment and front-end records
│   └── figures/                  # architecture diagram, nine bench screenshots
├── .github/workflows/tests.yml   # five suites on Linux, Python 3.12 and 3.13
├── requirements-dev.txt
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── README.zh-CN.md
```

The tree is a multi-component source tree rather than a single installable package; each component
carries its own requirements and tests.

## 4. Correspondence between builds

Four source sets exist. Their `worker.py` and `llm.py` digests separate them; the remaining 26 vision
modules are shared by the Pi and GPU builds.

| Source set | Location | `worker.py` | `llm.py` | Written |
|---|---|---|---|---|
| This repository (main tree) | `vision/lab/` | `19030c2a` | `19e20583` | 2026-11-18 17:03 (GPU) |
| GPU instance | `sources/gpu-20261120/` | `19030c2a` | `19e20583` | 2026-11-18 17:03 |
| Raspberry Pi | `sources/pi-20261120/` | `c394fad5` | `48f5d2f2` | 2026-11-18 10:55 |
| Control host | `sources/control-20261120/` | `9d48941e` | `48f5d2f2` | older base build |
| Earlier GPU baseline | `revisions/vision-base-20261120/` | `fe10e1e8` | — | 2026-11-16 |

Digests are computed on committed blobs after LF normalisation. The full per-file comparison, including
the front end, is in `docs/deployments-20261120.md` and `docs/frontend-20261120.md`.

## 5. Verification

### 5.1 Test suites

| Suite | Cases | Result | Command |
|---|---|---|---|
| Environment gateway | 35 | pass | `python -m unittest discover -s environment/tests` |
| Environment long-term revision | 36 | pass | `python -m unittest discover -s revisions/environment-longterm/tests` |
| Acoustic | 67 | pass, 5 skipped | `cd acoustic && python -m unittest discover -s tests` |
| Vision | 121 | pass, 7 skipped | `cp vision/config.example.json vision/config.json && cd vision && python -m unittest discover -s tests` |
| Collaboration overlay | 71 | pass | `cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests` |
| Total | 330 | 0 failures, 12 skipped | CI: `.github/workflows/tests.yml` |

The 5 acoustic skips require Sherpa-ONNX models that exist only on the GPU host. The 7 vision skips
exercise fixtures written for the earlier worker build and carry their reason in code.

### 5.2 Runtime verification

On 2026-11-20 at 22:16 JST, seven resident services were checked on the Pi, the GPU and the control
host: every process began after the last write of the package it executes, and the Pi's 50 source
files matched the captured digests. Of the 28 vision modules, 23 are byte-identical between the
archive and the device and 5 differ only in the substitutions applied during sanitisation. The
procedure and raw output are in `docs/runtime-verification.md`.

### 5.3 Reproducing the checks

```bash
git clone https://github.com/callmetse/thermal-lab.git
cd thermal-lab
python -m pip install -r requirements-dev.txt
```

Python 3.12 or newer is required; the suites run on Linux, macOS and Windows, and deployment targets
Linux.

## 6. Documentation

| Document | Content |
|---|---|
| [`docs/system-verification.md`](docs/system-verification.md) | Test evidence, build identities, evidence status, remaining checks |
| [`docs/runtime-verification.md`](docs/runtime-verification.md) | Process-level verification of the three tiers |
| [`docs/deployments-20261120.md`](docs/deployments-20261120.md) | Four-way deployment matrix and file-level differences |
| [`docs/frontend-20261120.md`](docs/frontend-20261120.md) | Bench routes and the three copies of the front end |
| [`docs/architecture.md`](docs/architecture.md) | A/B/C/D capture schemes and the shared cloud pipeline |
| [`docs/environment-gateway.md`](docs/environment-gateway.md) | Gateway contract with worked examples |
| [`docs/gpu-endpoint.md`](docs/gpu-endpoint.md) | Why the GPU endpoint changes and how it is recovered |
| [`docs/deployed-baseline.md`](docs/deployed-baseline.md) | Earlier baseline build, kept under `revisions/` |
| [`docs/pi-diff-20261120.md`](docs/pi-diff-20261120.md) | File-level comparison of repository and Pi |
| [`docs/figures/bench/README.md`](docs/figures/bench/README.md) | Nine bench screenshots and their capture conditions |

## 7. Boundaries

- **Not a deployment image.** The repository is a source and evidence record; it does not ship model
  weights, databases, recordings, logs or runtime state.
- **Sanitised.** Hosts, accounts, ports and key names were replaced with example values, so a local
  copy cannot reach the laboratory devices.
- **GPU endpoint instability.** The instance is rented, and its SSH port is reassigned at every start.
  Ports recorded here are valid only for the capture date; `docs/gpu-endpoint.md` describes recovery.
- **Front-end divergence.** Three copies of the bench page exist: a local edit in this repository
  (2026-11-16, 51,882 B), the page served by both devices (2026-11-03, 35,206 B), and a newer
  Node-RED flow on the control host (2026-11-17, 102,105 B). Which copy is authoritative is undecided.
- **ESP32.** Only the ESPHome definition is present; the firmware is neither built here nor flashed,
  and no end-to-end sensor validation is recorded.
- **Deployment scripts.** They are historical material and their host paths must be re-checked before
  use.

## 8. Contribution

Changes proceed through one branch, one commit and one pull request per issue. Test suites are
expected to remain green, and new deployment facts are recorded in `docs/system-verification.md`
rather than in correspondence. `CONTRIBUTING.md` states the conventions.

## 9. Licence

BSD-2-Clause. See [LICENSE](LICENSE).

## 10. Contact

Maintainer: [@callmetse](https://github.com/callmetse). 中文说明见 [README.zh-CN.md](README.zh-CN.md).
