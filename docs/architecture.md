# Four edge/cloud layouts sharing one cloud-side structure

![Four A/B/C/D architectures: edge decision, payload, transport channel, cloud compute and
control-plane sink](figures/abcd-architecture.png)

The four schemes (A/B/C/D) coexist. They differ only in **what the edge decides** and **what it
uploads**; the control plane and the media plane are two independent MQTT lanes, so control traffic
and media traffic do not compete. The learning loop runs offline, not online.

## The four schemes

| Scheme | Edge decision | Payload | Transport | Cloud compute | Control sink |
|---|---|---|---|---|---|
| **A** Local recognition | Edge model detects the person; only the bounding box is used | Cropped JPEG (`crops`), ≤2 Hz, only when the frame changes | Media 28583, QoS 0; control 28883 | Crop inference → posture / clothing / category | `crop_attrs` returned via the control plane; the edge reports observations itself |
| **B** Gated cloud upload | Gate model (NCNN yolo11n, imgsz 416) decides person presence; upload only when present | Whole frames | Media 28583, QoS 1, 6 frames/s cap | YOLO11-pose → ByteTrack → TCN | Observation store; the hard-example selector runs on the edge |
| **C** Full cloud upload (**current main**) | No edge inference; every frame is uploaded | Whole frames | Media 28583, QoS 1, 6 frames/s cap | YOLO11-pose → ByteTrack → TCN | Observation store + console + batch export |
| **D** Video replay | Frames come from an uploaded video file | Whole frames | Media 28583, QoS 1 | Same pipeline as C | Same ingest and export as C |

Two details the figure marks as not settled: whether the B gate and the A-line detector share
weights is **to be confirmed**, and D does not add an independent edge lane.

## Interface contract

All topics live under the root topic `labs/abc20261102/v1`; `clock_status = not_validated` means the
clock has not been validated.

| Topic | QoS | Direction | Payload | Trigger |
|---|---|---|---|---|
| `/commands` | 1 | control cloud → edge | session start/stop JSON | event driven |
| `/runs/+/events` | 1 | edge / compute cloud → control cloud | session state / heartbeat / events | state change + periodic |
| `/runs/+/frames` | 1 | edge → compute cloud | 4-byte header + JSON header + JPEG | sampling 12 fps (media lane) |
| `/runs/+/observations` | 1 | compute cloud → control cloud | observation JSON (tracks / bbox / conf) | measured 11.4 per second |
| `/runs/+/crops` | 0 | edge → compute cloud | 4-byte header + JSON header + JPEG | ≤2 Hz threshold, on frame change |
| `/runs/+/crop_attrs` | 0 | compute cloud → edge | box attribute JSON | paired with crops |

`frames`, `crops` and `observations` ride the media lane (28583); `commands`, `events` and
`crop_attrs` ride the control lane (28883).

## The B learning loop (offline)

```
S1 hard-example trigger → S2 bounded sample store → S3 export bundle → S4 manual annotation (CVAT)
    ↑                                                                              ↓
S6 candidate model  ←  S5 GPU training  ←  (manifest.json + samples/)
```

| Stage | Detail | Status in the figure |
|---|---|---|
| S1 trigger | `random_audit` every 15 s; `motion_without_person`; `gate_negative_but_motion` | running |
| S2 sample store | ≤50 MB or ≤500 samples, 7 days, content-hash dedup, on the edge | running |
| S3 export bundle | `manifest.json` + `samples/` | running |
| S4 manual annotation | CVAT | **BLOCKED** — manual import blocked |
| S5 GPU training | teacher yolo11n-pose labels; student yolo11n, first 10 layers frozen; 1 epoch; imgsz 416 | **BLOCKED** — single epoch, no independent test set |
| S6 candidate model | — | **UNTESTED** |

`BLOCKED` and `UNTESTED` are the status on 2026-11-20 and mean the release gate has not passed.

## Numbers

Ports are the listening ports actually observed. The measurements come from two 60-second sessions
on 2026-11-20 (11–18 and 11–42 observations/s, median inference latency 27.6 / 26.0 ms); the figure
uses the second run.

> The figure is the author's rendering, committed unchanged as
> `docs/figures/abcd-architecture.png` (SHA-256 `f66378d07b70df4b…`). Where it states a measurement
> or a status, this document repeats it as stated; it is not an independent verification.
