# Thermal Lab · Edge and cloud sensing system

Source review for the ESP32, Raspberry Pi, GPU worker and control host. Environmental sensors use HTTP; vision and audio use separate workers and MQTT.

| Role | Source | Function |
|---|---|---|
| ESP32 | [ESPHome definition](environment/firmware/env01.yaml), [publisher](environment/firmware/env_publish.h) | SHT4x and SCD41 readings and heartbeats |
| Pi environment | [HTTP gateway](environment/env_gateway.py), [Supervisor template](environment/supervisord.ini) | Validation, SQLite, latest/history API |
| Pi vision | [Worker](vision/lab/worker.py), [camera](vision/lab/camera.py) | Camera or replay, edge processing and media transport |
| Pi audio | [Audio edge](acoustic/edge.py), [wake word](acoustic/tools/wake_word.py), [UI](acoustic/page.html) | Capture, session control and MQTT events |
| GPU cloud | [Vision worker](vision/lab/worker.py), [audio worker](acoustic/worker.py), [Ollama example](vision/deploy/gpu.compose.yml) | Cloud inference and result delivery |
| Control cloud | [Node-RED flows](vision/deploy/nodered/flows-native.json), [broker and console](vision/deploy/control.compose.yml) | Session commands, results and operator console |
| Experiments | [Experiments and exports](vision/tools/), [monitoring and dataset extension](revisions/vision-extension/) | Run control, knowledge retrieval, comparison and reporting |

## Local review

Clone the private repository with an authorized GitHub account:

~~~sh
git clone --branch review/full-system-source https://github.com/callmetse/thermal-lab.git
cd thermal-lab
python -m pip install -r environment/requirements.txt
python -m unittest discover -s environment/tests -v
~~~

See the [environment gateway guide](docs/environment-gateway.md) for the tested HTTP example. For vision, work from `vision/`, copy `config.example.json` to `config.json` and install role-specific dependencies from `requirements-runtime.txt`. Start with `python -m lab.worker edge` on Pi or `python -m lab.worker cloud` on GPU after configuring devices, MQTT and model files. Audio runs from `acoustic/`; provision Sherpa-ONNX and the model files for the cloud worker.

## Provisioning and provenance

- Copy `environment/firmware/secrets.yaml.example` to `secrets.yaml` locally and set the Wi-Fi, OTA and gateway values. Compile for the actual ESP32 board. The device requires a reachable gateway address.
- Configure host addresses, SSH identities, network controls and credentials locally. The source uses loopback and example addresses. Browser checks requiring a password read `THERMAL_LAB_PASSWORD`. Historical provisioning scripts assume host-specific paths. Review them before use.
- Provision approved weights separately. See the [vision checksums](vision/MODEL_MANIFEST.json) and [audio model manifest](acoustic/MODEL_MANIFEST.json). VAD/KWS modes can require additional assets. Site databases, recordings, key material and compiled firmware are excluded.
- The two local application trees supply the base vision and audio modules. `revisions/vision-extension/` retains the later partial vision snapshot including dataset APIs, Node-RED session logic and monitoring. `revisions/environment-longterm/` preserves the alternative gateway. `revisions/collaboration/` retains a differing worker and its tests/tools. Compare revisions with the target device before applying an overlay.
- The audio deployment script generates `gateway.sh`. The Supervisor template also references `setup_capture.sh`; its standalone source was not found. The listener script expects `utt_listener.py` in the service root while the source keeps it under `acoustic/tools/`. Verify the Pi capture setup before enabling either target.
- See [verification and remaining checks](docs/system-verification.md) before deployment.
