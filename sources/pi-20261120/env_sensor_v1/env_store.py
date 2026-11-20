"""环境测量的本地存储：SQLite、单条去重、latest/history 查询。

保留原始值；平滑值只能是派生字段，不进这张表。
"""
import json
import sqlite3
import threading
import time
from contextlib import closing

import env_schema as S

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    event_id        TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    sensor_id       TEXT NOT NULL,
    zone_id         TEXT NOT NULL,
    boot_id         INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    measured_at_ms  INTEGER,
    received_at_ms  INTEGER NOT NULL,
    data_time_ms    INTEGER NOT NULL,
    device_uptime_ms INTEGER NOT NULL,
    clock_status    TEXT NOT NULL,
    values_json     TEXT NOT NULL,
    units_json      TEXT NOT NULL,
    quality         TEXT NOT NULL,
    error_code      TEXT,
    firmware_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meas_zone_time ON measurements(zone_id, data_time_ms);
CREATE INDEX IF NOT EXISTS idx_meas_device_boot ON measurements(device_id, boot_id, seq);

CREATE TABLE IF NOT EXISTS device_status (
    event_id        TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    zone_id         TEXT NOT NULL,
    boot_id         INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    received_at_ms  INTEGER NOT NULL,
    uptime_ms       INTEGER NOT NULL,
    clock_status    TEXT NOT NULL,
    wifi_rssi_dbm   INTEGER,
    ip              TEXT,
    firmware_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_zone_time ON device_status(zone_id, received_at_ms);
"""

DEVICE_ONLINE_MS = 60_000


class EnvStore:
    def __init__(self, path='env_sensor.sqlite3', clock=None):
        self.path = str(path)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: int(time.time() * 1000))
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    def now_ms(self):
        return self._clock()

    # ---- 写入 -------------------------------------------------------
    def insert(self, event, received_at_ms=None):
        """写入一条已经过 :func:`env_schema.validate_measurement` 的事件。

        返回 True 表示新写入，False 表示 event_id 重复（QoS1/重试的常态）。
        """
        received = self._clock() if received_at_ms is None else received_at_ms
        data_time = event.get('measured_at_ms') or received
        with self._lock, closing(self._connect()) as db:
            cur = db.execute(
                'INSERT OR IGNORE INTO measurements (event_id, device_id, sensor_id,'
                ' zone_id, boot_id, seq, measured_at_ms, received_at_ms, data_time_ms,'
                ' device_uptime_ms, clock_status, values_json, units_json, quality,'
                ' error_code, firmware_version)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event['event_id'], event['device_id'], event['sensor_id'],
                 event['zone_id'], event['boot_id'], event['seq'],
                 event['measured_at_ms'], received, data_time,
                 event['device_uptime_ms'], event['clock_status'],
                 json.dumps(event['values'], ensure_ascii=False, sort_keys=True),
                 json.dumps(event['units'], ensure_ascii=False, sort_keys=True),
                 event['quality'], event['error_code'],
                 event['firmware_version']))
            db.commit()
            return cur.rowcount > 0

    def insert_status(self, status, received_at_ms=None):
        received = self._clock() if received_at_ms is None else received_at_ms
        with self._lock, closing(self._connect()) as db:
            cur = db.execute(
                'INSERT OR IGNORE INTO device_status (event_id, device_id, zone_id,'
                ' boot_id, seq, received_at_ms, uptime_ms, clock_status,'
                ' wifi_rssi_dbm, ip, firmware_version)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (status['event_id'], status['device_id'], status['zone_id'],
                 status['boot_id'], status['seq'], received, status['uptime_ms'],
                 status['clock_status'], status['wifi_rssi_dbm'], status['ip'],
                 status['firmware_version']))
            db.commit()
            return cur.rowcount > 0

    # ---- 读取 -------------------------------------------------------
    def _latest_rows(self, zone_id, limit=400):
        with closing(self._connect()) as db:
            return db.execute(
                'SELECT * FROM measurements WHERE zone_id = ?'
                ' ORDER BY data_time_ms DESC, seq DESC LIMIT ?',
                (zone_id, limit)).fetchall()

    def latest(self, zone_id, now_ms=None, stale_after=None):
        """每个字段的"主来源"最近一次测量及其数据年龄。

        温湿度只认 SHT4x、CO₂ 只认 SCD41：SCD41 也会报温湿度，但它自热明显，
        混进来会让页面上的温度在 20.4 和 24.0 之间来回跳。SCD41 的温湿度单独放在
        ``cross_check`` 里，只做一致性诊断，不参与主字段。

        取的是主来源最近一条带该字段的事件，即使 ``quality`` 是 invalid——
        出错的读数必须显示为出错，不能拿更早的旧值顶替。完全没有数据时返回
        ``present=False``，页面显示"缺测"，不用 0 顶替。
        设备时钟不可靠时按网关接收时间计算年龄。
        """
        now = self._clock() if now_ms is None else now_ms
        thresholds = dict(S.STALE_AFTER_MS)
        thresholds.update(stale_after or {})
        rows = self._latest_rows(zone_id)
        parsed = [(row, json.loads(row['values_json'])) for row in rows]

        fields = {}
        for field in S.FIELDS:
            primary = S.PRIMARY_SENSOR[field]
            chosen = None
            for row, values in parsed:
                if row['sensor_id'] == primary and field in values:
                    chosen = (row, values[field])
                    break
            if chosen is None:
                fields[field] = {
                    'field': field, 'value': None, 'unit': S.FIELDS[field][0],
                    'present': False, 'stale': True, 'age_ms': None,
                    'quality': None, 'error_code': None, 'sensor_id': primary,
                    'event_id': None, 'data_time_ms': None,
                    'clock_status': None, 'source': 'missing',
                }
                continue
            row, value = chosen
            age = now - row['data_time_ms']
            fields[field] = {
                'field': field, 'value': value, 'unit': S.FIELDS[field][0],
                'present': True, 'stale': age > thresholds[field], 'age_ms': age,
                'quality': row['quality'], 'error_code': row['error_code'],
                'sensor_id': row['sensor_id'], 'event_id': row['event_id'],
                'data_time_ms': row['data_time_ms'],
                'clock_status': row['clock_status'],
                'source': 'device_clock' if row['clock_status'] == 'synced'
                          else 'gateway_receive_time',
            }

        cross_check = {}
        for sensor_id, sensor_fields in S.CROSS_CHECK_SENSOR.items():
            for field in sensor_fields:
                for row, values in parsed:
                    if row['sensor_id'] == sensor_id and field in values:
                        cross_check['%s_by_%s' % (field, sensor_id)] = {
                            'field': field, 'value': values[field],
                            'unit': S.FIELDS[field][0], 'sensor_id': sensor_id,
                            'age_ms': now - row['data_time_ms'],
                            'quality': row['quality'],
                            'note': 'consistency_check_only',
                        }
                        break

        device = self.device(zone_id, now)
        return {'zone_id': zone_id, 'now_ms': now, 'fields': fields,
                'cross_check': cross_check, 'device': device,
                'stale_after_ms': thresholds, 'schema_version': S.SCHEMA_VERSION}

    def device(self, zone_id, now_ms=None):
        now = self._clock() if now_ms is None else now_ms
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT * FROM device_status WHERE zone_id = ?'
                ' ORDER BY received_at_ms DESC LIMIT 1', (zone_id,)).fetchone()
        if row is None:
            return {'device_id': None, 'online': False, 'last_seen_ms': None,
                    'age_ms': None, 'ip': None, 'wifi_rssi_dbm': None,
                    'firmware_version': None, 'clock_status': None}
        age = now - row['received_at_ms']
        return {'device_id': row['device_id'], 'online': age <= DEVICE_ONLINE_MS,
                'last_seen_ms': row['received_at_ms'], 'age_ms': age,
                'ip': row['ip'], 'wifi_rssi_dbm': row['wifi_rssi_dbm'],
                'firmware_version': row['firmware_version'],
                'clock_status': row['clock_status']}

    def history(self, zone_id, since_ms=None, until_ms=None, field=None,
                limit=1000):
        now = self._clock()
        since = now - 3600_000 if since_ms is None else since_ms
        until = now if until_ms is None else until_ms
        sql = ('SELECT event_id, sensor_id, data_time_ms, values_json, units_json,'
               ' quality, error_code, clock_status FROM measurements'
               ' WHERE zone_id = ? AND data_time_ms >= ? AND data_time_ms <= ?')
        params = [zone_id, since, until]
        if field is not None:
            if field not in S.FIELDS:
                raise ValueError('未知字段: %s' % field)
            sql += " AND json_extract(values_json, '$.%s') IS NOT NULL" % field
        sql += ' ORDER BY data_time_ms ASC LIMIT ?'
        params.append(int(limit))
        with closing(self._connect()) as db:
            rows = db.execute(sql, params).fetchall()
        out = []
        for row in rows:
            values = json.loads(row['values_json'])
            if field is not None and field not in values:
                continue
            out.append({
                'event_id': row['event_id'], 'sensor_id': row['sensor_id'],
                'data_time_ms': row['data_time_ms'],
                'value': values.get(field) if field else values,
                'unit': S.FIELDS[field][0] if field else None,
                'quality': row['quality'], 'error_code': row['error_code'],
                'clock_status': row['clock_status'],
            })
        return out

    def prune(self, older_than_ms):
        with self._lock, closing(self._connect()) as db:
            cur = db.execute('DELETE FROM measurements WHERE data_time_ms < ?',
                             (older_than_ms,))
            db.commit()
            return cur.rowcount

    def stats(self):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0]
            devices = db.execute('SELECT COUNT(DISTINCT device_id) FROM measurements').fetchone()[0]
            status = db.execute('SELECT COUNT(*) FROM device_status').fetchone()[0]
        return {'measurements': rows, 'devices': devices, 'status_events': status}
