# What each tier runs (2026-11-20)

Read-only captures of the three online tiers, taken on 2026-11-20. Nothing on any device was
modified. Sanitised archives live under `deployments/`.

| Tier | Entry | Captured | Archive |
|---|---|---|---|
| Raspberry Pi | `pi@192.168.5.8` (SSH), `http://192.168.5.8:28880` | source, Supervisor configs, console files | `deployments/pi-20261120/` (149 files) |
| GPU cloud | `root@connect.nmb2.seetacloud.com:31380` | source, Supervisor config, vendor code | `deployments/gpu-20261120/` (41 files) |
| Control host | `root@<control>` | Node-RED deployment, units, code, research notes | `deployments/control-20261120/` (69 files) |

## Service state observed (2026-11-20 ~21:55)

- **GPU** — container `db94499d91`, RTX 3090 24 GB, project `/root/abc20261102`, five Supervisor
  programs RUNNING: `vision-cloud`, `llm-adapter`, `ollama`, `mqtt-tunnel`, `media-broker`.
- **Pi** — vision Supervisor runs six programs RUNNING: `vision-edge`, `console-gateway`,
  `console-tunnel`, `dataset-api`, `media-tunnel`, `mqtt-tunnel`. Audio `edge.py` and the Caddy
  gateway process are running, with the `audio-profile` program configured; the environment
  gateway runs from the vision virtualenv and listens on `:28984`. Listening sockets observed:
  `:28880` (Caddy), `:28990` (vision edge), `:28992` (data plane), `:28881` (console tunnel),
  `:28883` (MQTT tunnel), `:28583` (media transport), `:28984` (environment). Two further
  listeners, `:28980` and `:28991`, were observed but are not attributed to a component here.
- **Control host** — `abc20261102-broker`, `abc20261102-console` and `abc20261102-gpu-ensure.timer`
  are active; `:28880` is listening; Node-RED user dir `/srv/abc20261102/state/node-red`.

## Vision package: four deployments, five worker builds

The `lab/` package is present on the Pi and GPU (extended), on the control host (ten files), and in
this repository (base). Files that are identical everywhere: `__init__.py`, `camera.py`,
`gpu_health.py`, `hard_examples.py`, `posture.py`, `recognition_region.py`, `result_codec.py`,
`sample_store.py`, `scene_gate.py`, `transfer_policy.py`.

Files that differ, by SHA-256 prefix:

| File | Repository | Pi | GPU | Control |
|---|---|---|---|---|
| `worker.py` | `fe10e1e8` | `c394fad5` | `19030c2a` | `9d48941e` |
| `common.py` | `f7e539fb` | `30d0d36e` | `30d0d36e` | `f48d407a` |
| `llm.py` | `48f5d2f2` | `48f5d2f2` | `19e20583` | `48f5d2f2` |
| `local_preview.py` | `ae7cab33` | `6201c680` | `6201c680` | `f401854d` |
| `presentation.py` | `c9af91c8` | `c9af91c8` | `c9af91c8` | `ff44bad2` |
| `vision.py` | `8c8cd7d1` | `8c8cd7d1` | `8c8cd7d1` | `6bf00149` |
| `collection_batches.py` | `bba36c5b` | `81f0e146` | `81f0e146` | — |
| `collection_routes.py` | `b99c4377` | `cee7deaa` | `cee7deaa` | — |
| `experiment_archive.py` | `9cbcc73f` | `07b943c7` | `07b943c7` | — |
| `video_uploads.py` | `e0afbee4` | `f5898c91` | `f5898c91` | — |

Eight modules exist on the Pi and GPU but not in the base tree: `aggregation.py`,
`archive_ingest.py`, `crop_infer.py`, `dataset_api.py`, `dataset_settings.py`, `model_manifest.py`,
`object_line.py`, `record_schema.py`. All eight, and most of the differing files above, match
`revisions/vision-extension/lab/` exactly; the exception is `worker.py`, which matches no copy.

Reading:

- **Pi and GPU run the same extended build** apart from `worker.py` and `llm.py`.
- **The control host runs an older base build** with its own edits, not the extended one.
- **This repository holds the base build**; its extended archive under `revisions/` is close to what
  the Pi and GPU run but is not identical (`worker.py`).
- **Five distinct `worker.py` builds** were observed: the repository copy, the Pi, the GPU, the
  control host, and the 2026-11-16 GPU baseline (`c40d2040`). The file name alone identifies none
  of them.

## Front end

The experiment bench is covered separately: see [frontend-20261120.md](frontend-20261120.md).

## Not captured

- Model weights, databases, recordings and local `config.json` files (deliberately excluded).
- ESP32 firmware build output; only the ESPHome definition is in the tree.
- Anything the tiers hold outside the paths above (`data/`, `state/`, `results/`, `logs/`).

## Refreshing these captures

Each archive carries a `SHA256SUMS` of its sanitised files. To re-capture, repeat the read-only
`find` + `sha256sum` and the `tar` transfer, then re-run the sanitiser and the comparison described
in `system-verification.md`. Compare digests of the **committed blob** (LF), not a CRLF working copy.
