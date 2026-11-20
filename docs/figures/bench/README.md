# Experiment bench — screenshots (2026-11-20)

Screenshots of the bench as it was actually serving on 2026-11-20 between 22:50 and 22:54
(Asia/Tokyo), captured through the LAN entry `http://<pi>:28880` with the bench's own LAN basic
auth, at a 1600×1100 viewport (full page).

They are UI evidence: they show what the browser is served and how a live run looks. They are not
deployment proof — that is what `docs/runtime-verification.md` and `sources/` are for.

| File | Page | State when captured | What it shows |
|---|---|---|---|
| `bench-console-idle.png` | `/lab` | idle, no run | plan selector (C selected), object filter, capture location / batch name, duration, confidence window, scan region, cross-line direction, 12 fps; live environment block (19.64 °C, 40.7 %RH, 450 ppm, `env01` online); empty preview panel |
| `bench-console-running.png` | `/lab` | run active (60 s, stopped after capture) | same page while a run is live: `C 通道运行中`, live camera frame with the person box, recognised attributes (hat / glasses / hoodie), environment values updating (`最近测量 1.8 秒前`), batch row `工位A · 导师审查-实验台截图 · 2026/11/20 22:53:20 · 10 条` |
| `bench-data-query.png` | `/data` | idle | environment long-term records (three curves, 20 s interval, last segment 2026/11/17 01:07:21–01:07:51), batch list with per-batch records and the download / archive / trash actions |

## How they were taken

Headless Chrome (Playwright) with the LAN credentials, `wait_until="networkidle"` plus a settle
delay so the charts and the preview WebSocket had time to paint. The run in
`bench-console-running.png` was started from the page itself (location `工位A`, batch
`导师审查-实验台截图`, 60 s) and stopped once the frame was captured; the two batches it created
stay in the bench data and are visible in `bench-data-query.png`'s list.

## Privacy

The live frame contains the operator. The face is blurred (Gaussian, mask clipped to the video
panel) before publication, so the screenshot shows the detection box and the recognised
attributes without showing the person's face.

## What these screenshots do not show

- They do not prove which files the devices run; see `docs/frontend-20261120.md` for the three
  `page.html` copies and which one is being served.
- The environment block is live sensor data from `env01` through the gateway on `:28984`; values
  are a single moment, not a calibrated measurement.
