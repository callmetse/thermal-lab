# Thermal Lab · Environment Gateway

The gateway receives ESP32 sensor readings and device heartbeats over HTTP, validates each event, and writes it to SQLite. Clients can query the latest temperature, relative humidity and CO₂ concentration, or request a time window of measurements.

See the repository root for ESP32 firmware, Pi launch configuration and the cloud services.

## Run locally

You need Git, Python 3.13 (the version used for acceptance), and a writable database directory. Clone the system review branch to get the gateway:

```sh
git clone --branch review/full-system-source https://github.com/callmetse/thermal-lab.git
cd thermal-lab
```

From the repository root, run the following in **Windows PowerShell**:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r environment/requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s environment/tests -v
.\.venv\Scripts\python.exe environment/env_gateway.py --host 127.0.0.1 --port 28984 --db environment/local.sqlite3
```

On **Linux or macOS**, create the environment with `python3 -m venv .venv` and use `.venv/bin/python` for the remaining commands. Stop the server with Ctrl+C. SQLite creates the database on first use and reopens it on restart.

## Check ingestion and retry handling

Start with a fresh database and the local command above (write authentication disabled). In a second PowerShell window, send one synthetic reading twice:

```powershell
$base = 'http://127.0.0.1:28984/env/v1'
Invoke-RestMethod "$base/health"
$reading = @{
    schema_version = 1; device_id = 'demo'; sensor_id = 'sht4x'; zone_id = 'lab'
    boot_id = 1; seq = 1; event_id = 'demo-1-1'; device_uptime_ms = 2000
    clock_status = 'unsynced'; measured_at_ms = $null
    values = @{temperature_c = 22.5}; units = @{temperature_c = 'degC'}
    quality = 'ok'; error_code = $null; firmware_version = 'local-demo'
} | ConvertTo-Json
$request = @{Uri = "$base/measurements"; Method = 'Post'; ContentType = 'application/json'; Body = $reading}
Invoke-RestMethod @request
Invoke-RestMethod @request
(Invoke-RestMethod "$base/latest?zone_id=lab").fields.temperature_c
Invoke-RestMethod "$base/history?zone_id=lab&field=temperature_c&minutes=5"
```

The first POST returns HTTP 201 with `duplicate: false`. The retry returns HTTP 200 with `duplicate: true`. Latest reports 22.5 °C, and history contains one reading for this event. Both measurements and heartbeats use `device_id-boot_id-seq` as the event ID; each table deduplicates independently.

## HTTP interface

All paths are relative to `/env/v1`. POST bodies are JSON. An invalid measurement can be retained with `quality: invalid`; a malformed event returns HTTP 400.

| Method and path | Contract |
|---|---|
| POST `/measurements` | Submit a version 1 sensor event. SHT4x is the primary source for temperature and humidity; SCD41 is the primary source for CO₂. |
| POST `/status` | Submit a version 1 heartbeat with device, zone, boot, sequence, event ID, uptime, clock status and firmware version. A heartbeat received within 60 seconds marks its device online. |
| GET `/latest` | Optional `zone_id` (default `lab`). Reports fields, age, quality, freshness and the latest device status. Selection considers the newest 400 measurement rows in the zone. |
| GET `/history` | Accepts `zone_id`, `field`, `since_ms`, `until_ms`, `minutes` and `limit`. Timestamps are Unix milliseconds. Defaults to the last hour and 1,000 rows; maximum 10,000. Rows are oldest first. |
| GET `/health` | Returns schema version, authentication setting, freshness thresholds and database row counts. |

## Operation and acceptance

- **Network and access.** The example binds to loopback. Omitting `--host` binds to `0.0.0.0`. Set `ENV_SENSOR_TOKEN` in the server environment to require the `X-Env-Token` header on both POST routes. GET routes remain readable without a token. Restrict network access and provide TLS before sending tokens across untrusted networks.
- **Storage.** The default file is `environment/env_sensor.sqlite3` beside the script. SQLite uses WAL mode. At startup the gateway prunes measurements older than `--retention-days` (default 30); zero disables pruning. Device status rows are retained. Use SQLite's backup facility for a live database.
- **Code and tests.** [HTTP routes and CLI](../environment/env_gateway.py), [event schema](../environment/env_schema.py), [storage and queries](../environment/env_store.py), [test suite](../environment/tests/).
- **Acceptance scope.** Verified in a clean Windows checkout with Python 3.13 and aiohttp 3.14.5: dependency check passed, all 35 tests passed, and the example returned 201 then 200. Latest reported 22.5 °C; history and SQLite each contained one measurement. The database passed its integrity check after process exit. ESP32 delivery, Raspberry Pi service startup and long-running operation require device-side validation.
