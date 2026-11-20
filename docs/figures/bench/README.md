# Experiment bench — screenshots (2026-11-20)

Screenshots of the bench as it was actually serving on 2026-11-20 between 22:50 and 23:01
(Asia/Tokyo), captured through the LAN entry `http://<pi>:28880` with the bench's own LAN basic
auth, 1400–1600 px wide (full page or cropped to the panel named below).

They are UI evidence: they show what the browser is served and how a live run looks. They are not
deployment proof — that is what `docs/runtime-verification.md` and `sources/` are for.

## `/lab` — experiment console (control plane)

| File | State when captured | What it shows |
|---|---|---|
| `bench-console-idle.png` | idle, no run | plan selector (C selected), object filter, capture location / batch name, duration, confidence window, scan region, cross-line direction, 12 fps; live environment block (19.64 °C, 40.7 %RH, 450 ppm, `env01` online) |
| `bench-console-running.png` | run active (60 s, stopped after capture) | same page while a run is live: `C 通道运行中`, live camera frame with the person box, recognised attributes (hat / glasses / hoodie), environment values updating, batch row `工位A · 导师审查-实验台截图 · 2026/11/20 22:53:20 · 10 条` |

## `/data` — data plane (`:28992` behind the same entry)

| File | Panel | What it shows |
|---|---|---|
| `bench-data-query.png` | whole page | overview: environment curves, filters, batch list, actions |
| `bench-data-env.png` | 环境长期记录 | interval setting (20 s + unit + apply / restore), last segment, three curves (temperature, humidity, CO₂) with axes and the coverage note |
| `bench-data-batches.png` | filters + batch list | keyword search, date range, channel filter, view selector (`全部实验 / 我的收藏 / 回收站`), selection counter, bulk favourite / unfavourite / delete / restore, "remove moves to the recycle bin, recoverable" note, 7 batches with per-batch record count and actions (`查看数据`, `实验摘要`, download complete table, download JSON package, favourite, delete) |
| `bench-data-summary.png` | 实验摘要 of one batch | archived recognition results (261 rows, at most 1 person per frame, 0 cross-line events), per-sensor min / average / max with valid-value counts for both `sht4x` and `scd41` |
| `bench-data-records.png` | 环境记录预览 | first 20 raw records of the batch: receive time, sensor id, temperature, humidity, CO₂, with blank cells where a sensor did not report that quantity |
| `bench-data-trash.png` | 回收站 view | 95 batches in the recycle bin, each with `恢复`, plus the bulk restore action |
| `bench-data-api.png` | 开发者调用 · 接口地址 | the same endpoints the page calls: environment latest, batch list, per-batch records (paged with `next_after_id`), CSV export; note that they use the page's account and HTTP Basic auth |

## How they were taken

Headless Chrome (Playwright) with the LAN credentials, `wait_until="networkidle"` plus a settle
delay so the charts and the preview WebSocket had time to paint; then a full-page screenshot and,
for the panel shots, a crop to the panel's bounding box. The run in `bench-console-running.png` was
started from the page itself (location `工位A`, batch `导师审查-实验台截图`, 60 s) and stopped once
the frame was captured; the two batches it created stay in the bench data and appear in
`bench-data-batches.png`.

## Privacy and sanitisation

- The live frame contains the operator. The face is blurred (Gaussian, mask clipped to the video
  panel) before publication, so the detection box and the recognised attributes stay visible
  without the person's face.
- In `bench-data-api.png` the host in the endpoint URLs is masked to `192.0.2.11`, matching the
  sanitisation used for the rest of the repository; the real value is the bench's LAN address.

## What these screenshots do not show

- They do not prove which files the devices run; see `docs/frontend-20261120.md` for the three
  `page.html` copies and which one is being served.
- The environment block is live sensor data from `env01` through the gateway on `:28984`; the
  values are single moments, not calibrated measurements.
- The screenshots come from the LAN entry (`:28880`), which proxies `/data` to `:28992`; the data
  API itself is not reachable from outside the Pi.
