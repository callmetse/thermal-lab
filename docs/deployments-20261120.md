# What each tier runs (2026-11-20)

Read-only captures of the three online tiers, taken on 2026-11-20. Nothing on any device was
modified. Sanitised archives live under `deployments/`.

| Tier | Entry | Captured | Archive |
|---|---|---|---|
| Raspberry Pi | `pi@192.168.5.8` (SSH), `http://192.168.5.8:28880` | source, Supervisor configs, console files | `deployments/pi-20261120/` (152 files + `SHA256SUMS`, re-captured 22:32 JST) |
| GPU cloud | `root@connect.nmb2.seetacloud.com:31380` — port valid on 2026-11-20 only; it changes on every boot, see [gpu-endpoint.md](gpu-endpoint.md) | source, Supervisor config, vendor code | `deployments/gpu-20261120/` (41 files + `SHA256SUMS`) |
| Control host | `root@<control>` | Node-RED deployment, units, code, research notes | `deployments/control-20261120/` (69 files + `SHA256SUMS`) |

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

## Vision package: the repository carries the newest build

`vision/lab/` has 28 modules. `worker.py` and `llm.py` are the GPU instance's versions (written
2026-11-18 17:03:42, the latest observed); every other module is byte-identical on the Pi and the GPU.
Hashes below are of the sanitised copies, first 12 hex digits:

| File | Repository (= GPU) | Pi | Control |
|---|---|---|---|
| `worker.py` | `19030c2a0453` | `c394fad5ed7f` | `9d48941e3e21` |
| `llm.py` | `19e20583a5f1` | `48f5d2f2361d` | `48f5d2f2361d` |
| `common.py` | `30d0d36e6f5d` | `30d0d36e6f5d` | `f48d407aa1ee` |
| `local_preview.py` | `6201c680a5a6` | `6201c680a5a6` | `f401854d6ff7` |
| `presentation.py` | `c9af91c865a8` | `c9af91c865a8` | `ff44bad2376a` |
| `vision.py` | `8c8cd7d12418` | `8c8cd7d12418` | `6bf00149cd8c` |
| `collection_batches.py` | `81f0e1460a20` | `81f0e1460a20` | — |
| `collection_routes.py` | `cee7deaa2bfd` | `cee7deaa2bfd` | — |
| `experiment_archive.py` | `07b943c7bb3d` | `07b943c7bb3d` | — |
| `video_uploads.py` | `f5898c91cf1e` | `f5898c91cf1e` | — |

All other modules are identical across the three tiers.

Reading:

- **The repository matches the GPU exactly.** The Pi runs the previous pair of `worker.py` and
  `llm.py` (written 10:55 the same day, three hours earlier); nothing else differs between them.
- **The control host runs an older base build**: ten modules, five of which differ from the current
  tree, and it lacks the extended modules entirely.
- **`worker.py` has four builds in the records**: repository/GPU `19030c2a`, Pi `c394fad5`, control
  `9d48941e`, and the 2026-11-16 GPU baseline `c40d2040`. The old base build (`fe10e1e8`) is
  archived at `revisions/vision-base-20261120/`.
- **`revisions/vision-extension/`** matches the deployed build for 18 of 28 modules; the exception is
  `worker.py`.

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
