# The GPU endpoint changes on every boot

The GPU tier runs on a rented AutoDL instance. **AutoDL assigns a new SSH port each time the
instance starts, and the relay hostname can change with it.** Any port written into a document,
script or snapshot is only valid for that capture. Do not hard-code it.

## Observed endpoints

| Date / time (JST) | Relay host | Port | Evidence |
|---|---|---|---|
| until 2026-11-10 | `connect.nmb2.seetacloud.com` | 12434 | `GPU重部署记录_20261110.md` |
| 2026-11-16 morning | `connect.nmb1.seetacloud.com` | 23860 | `SSH记录_实例台账_20261116.md` §6 |
| 2026-11-16 15:00 | `connect.nmb1.seetacloud.com` | 33009 | same ledger, §1 and §2.4 |
| 2026-11-20 21:46 | `connect.nmb2.seetacloud.com` | 31380 | capture in `deployments/gpu-20261120/` |

`connect.nmb1.seetacloud.com:17005` and the key `autodl_yolo_17005` belong to a **different**
experiment instance and are not part of this project.

## What is stable across restarts

- the container identity observed on 2026-11-20: `db94499d91`
- project root `/root/abc20261102`
- login user `root` and key `autodl_nmb2_12434`
- five Supervisor programs: `vision-cloud`, `llm-adapter`, `ollama`, `mqtt-tunnel`, `media-broker`

None of these is a connection string; the port still has to be looked up.

## How to find the current port

1. AutoDL console → the instance page → the SSH login command it shows.
2. Or without the console: on the Pi, the vision Supervisor config carries the port in its
   `media-tunnel` command —
   `grep -n 'media-tunnel' -A2 /home/pi/abc20261102/deploy/supervisord.ini` — and the tunnel status
   shows whether it is actually up.
3. Confirm before capturing:
   `ssh -p <port> -i <key> root@<host> hostname` and `supervisorctl -c /root/abc20261102/deploy/supervisord.ini status`.

## Consequences

- Endpoints in this repository are **timestamped evidence**, not configuration.
- Automation must read the port at run time; a stale port fails as `Connection refused`, which is
  what happened on 2026-11-20 when the Pi still carried `12434` and the instance had moved to
  `31380`.
- When the port changes, both the Pi `media-tunnel` and the control-host `gpu-ensure` service must
  be updated, or the media path and the auto-recovery path break.
- A snapshot under `deployments/` records what was running **at capture time**; it does not imply
  the instance is reachable now.
