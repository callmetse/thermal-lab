"""环境测量事件的版本化契约（env_sensor_v1）。

只做校验与规范化，不连接任何服务；gateway / 测试共用这一份规则。

两条不可让步的规则（见 给DS_ESP32环境采集与多模态接入大纲_20261105.md §5）：

1. 超范围或非有限的值 **保留原值** 并把事件标成 ``quality="invalid"``，
   绝不裁剪成"看起来合理"的数字，也不把缺失写成 0。
2. 设备没完成对时（``clock_status="unsynced"``）时 ``measured_at_ms`` 必须是
   null，只有网关的接收时间可以当作数据时间使用。
"""
import math
import re

SCHEMA_VERSION = 1

IDENT = re.compile(r'^[a-zA-Z0-9_-]{1,40}$')

# 字段 -> (单位, 合法下限, 合法上限)
FIELDS = {
    'temperature_c': ('degC', -40.0, 85.0),
    'humidity_rh': ('%RH', 0.0, 100.0),
    'co2_ppm': ('ppm', 0.0, 40000.0),
}

# 传感器 -> 它负责的字段。SCD41 自带的温湿度只作一致性对照，
# 不参与主环境温湿度，也不能与 SHT4x 求平均。
SENSOR_FIELDS = {
    'sht4x': ('temperature_c', 'humidity_rh'),
    'scd41': ('co2_ppm', 'temperature_c', 'humidity_rh'),
}

SENSOR_LABELS = {'sht4x': 'SHT4x', 'scd41': 'SCD41'}

# 每个字段的"主来源"。SCD41 也能测温湿度，但它自热明显，只能当一致性对照，
# 不能和 SHT4x 混着取"最近一条"，否则页面上的温度会在两个传感器之间来回跳。
PRIMARY_SENSOR = {'temperature_c': 'sht4x', 'humidity_rh': 'sht4x', 'co2_ppm': 'scd41'}

# 只做一致性诊断的对照字段：来源传感器 -> 能提供的字段
CROSS_CHECK_SENSOR = {'scd41': ('temperature_c', 'humidity_rh')}

CLOCK_STATUSES = ('synced', 'unsynced')
QUALITIES = ('ok', 'invalid')

# 数据年龄超过这个阈值就视为过期（工程配置，可后续按实测调整）。
STALE_AFTER_MS = {'temperature_c': 10_000, 'humidity_rh': 10_000, 'co2_ppm': 20_000}

# 设备时间最多允许比网关时间快这么多，超过即判为时钟异常。
CLOCK_AHEAD_TOLERANCE_MS = 60_000


class SchemaError(ValueError):
    """事件不符合契约。不可修补的字段一律拒收，不做猜测性修正。"""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ident(raw, name):
    if not isinstance(raw, str) or not IDENT.match(raw):
        raise SchemaError('%s 必须是 1-40 位的字母/数字/_/-' % name)
    return raw


def event_id_for(device_id, boot_id, seq):
    return '%s-%d-%d' % (device_id, boot_id, seq)


def validate_measurement(raw, now_ms=None):
    """校验并规范化一条测量事件。

    返回规范化后的新 dict；不合契约时抛 :class:`SchemaError`。
    超范围的值不抛异常，而是把 ``quality`` 降级为 ``invalid`` 并给出 ``error_code``。
    """
    if not isinstance(raw, dict):
        raise SchemaError('事件必须是 JSON 对象')

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise SchemaError('未知字段: %s' % ', '.join(sorted(unknown)))

    for key in ('schema_version', 'device_id', 'sensor_id', 'zone_id', 'boot_id',
                'seq', 'event_id', 'device_uptime_ms', 'measured_at_ms',
                'clock_status', 'values', 'units', 'quality', 'error_code',
                'firmware_version'):
        if key not in raw:
            raise SchemaError('缺少字段: %s' % key)

    if raw['schema_version'] != SCHEMA_VERSION:
        raise SchemaError('schema_version 必须是 %d' % SCHEMA_VERSION)

    device_id = _ident(raw['device_id'], 'device_id')
    zone_id = _ident(raw['zone_id'], 'zone_id')
    sensor_id = raw['sensor_id']
    if sensor_id not in SENSOR_FIELDS:
        raise SchemaError('未知 sensor_id: %r（允许 %s）'
                          % (sensor_id, ', '.join(sorted(SENSOR_FIELDS))))

    boot_id = raw['boot_id']
    if not _is_int(boot_id) or not 0 <= boot_id <= 0xFFFFFFFF:
        raise SchemaError('boot_id 必须是 0..2^32-1 的整数')
    seq = raw['seq']
    if not _is_int(seq) or seq < 1:
        raise SchemaError('seq 必须是 >= 1 的整数')

    expected_id = event_id_for(device_id, boot_id, seq)
    if raw['event_id'] != expected_id:
        raise SchemaError('event_id 必须是 %r，收到 %r'
                          % (expected_id, raw['event_id']))

    uptime = raw['device_uptime_ms']
    if not _is_int(uptime) or uptime < 0:
        raise SchemaError('device_uptime_ms 必须是 >= 0 的整数')

    clock_status = raw['clock_status']
    if clock_status not in CLOCK_STATUSES:
        raise SchemaError('clock_status 必须是 %s 之一' % '/'.join(CLOCK_STATUSES))
    measured_at = raw['measured_at_ms']
    if clock_status == 'synced':
        if not _is_int(measured_at):
            raise SchemaError('clock_status=synced 时 measured_at_ms 必须是整数')
    elif measured_at is not None:
        raise SchemaError('clock_status=unsynced 时 measured_at_ms 必须是 null')

    values_raw = raw['values']
    units_raw = raw['units']
    if not isinstance(values_raw, dict) or not values_raw:
        raise SchemaError('values 必须是非空对象')
    if not isinstance(units_raw, dict):
        raise SchemaError('units 必须是对象')

    allowed = SENSOR_FIELDS[sensor_id]
    unexpected = set(values_raw) - set(allowed)
    if unexpected:
        raise SchemaError('%s 不提供字段: %s'
                          % (sensor_id, ', '.join(sorted(unexpected))))
    if set(units_raw) != set(values_raw):
        raise SchemaError('units 必须与 values 一一对应')

    values, units = {}, {}
    error_code = raw['error_code']
    quality = raw['quality']
    if quality not in QUALITIES:
        raise SchemaError('quality 必须是 %s 之一' % '/'.join(QUALITIES))

    for name, value in values_raw.items():
        unit, low, high = FIELDS[name]
        if units_raw[name] != unit:
            raise SchemaError('%s 的单位必须是 %s，收到 %r'
                              % (name, unit, units_raw[name]))
        if not _is_number(value):
            raise SchemaError('%s 必须是数字，收到 %r' % (name, value))
        value = float(value)
        if not math.isfinite(value):
            quality, error_code = 'invalid', 'non_finite'
            values[name] = None
            units[name] = unit
            continue
        if not low <= value <= high:
            quality, error_code = 'invalid', 'out_of_range'
        values[name] = value
        units[name] = unit

    if quality == 'invalid' and not isinstance(error_code, str):
        raise SchemaError('quality=invalid 时必须给出 error_code')
    if quality == 'ok' and error_code is not None:
        raise SchemaError('quality=ok 时 error_code 必须为 null')

    if (clock_status == 'synced' and now_ms is not None
            and measured_at > now_ms + CLOCK_AHEAD_TOLERANCE_MS):
        quality, error_code = 'invalid', 'clock_ahead'

    firmware_version = raw['firmware_version']
    if not isinstance(firmware_version, str) or not firmware_version:
        raise SchemaError('firmware_version 必须是非空字符串')

    return {
        'schema_version': SCHEMA_VERSION,
        'device_id': device_id,
        'sensor_id': sensor_id,
        'zone_id': zone_id,
        'boot_id': boot_id,
        'seq': seq,
        'event_id': expected_id,
        'device_uptime_ms': uptime,
        'measured_at_ms': measured_at,
        'clock_status': clock_status,
        'values': values,
        'units': units,
        'quality': quality,
        'error_code': error_code,
        'firmware_version': firmware_version,
    }


_ALLOWED_KEYS = {
    'schema_version', 'device_id', 'sensor_id', 'zone_id', 'boot_id', 'seq',
    'event_id', 'device_uptime_ms', 'measured_at_ms', 'clock_status', 'values',
    'units', 'quality', 'error_code', 'firmware_version',
}


def validate_status(raw, now_ms=None):
    """校验设备心跳/状态事件。"""
    if not isinstance(raw, dict):
        raise SchemaError('状态事件必须是 JSON 对象')
    for key in ('schema_version', 'device_id', 'zone_id', 'boot_id', 'seq',
                'event_id', 'uptime_ms', 'clock_status', 'firmware_version'):
        if key not in raw:
            raise SchemaError('缺少字段: %s' % key)
    if raw['schema_version'] != SCHEMA_VERSION:
        raise SchemaError('schema_version 必须是 %d' % SCHEMA_VERSION)

    device_id = _ident(raw['device_id'], 'device_id')
    zone_id = _ident(raw['zone_id'], 'zone_id')
    boot_id = raw['boot_id']
    seq = raw['seq']
    if not _is_int(boot_id) or not 0 <= boot_id <= 0xFFFFFFFF:
        raise SchemaError('boot_id 必须是 0..2^32-1 的整数')
    if not _is_int(seq) or seq < 1:
        raise SchemaError('seq 必须是 >= 1 的整数')
    expected_id = event_id_for(device_id, boot_id, seq)
    if raw['event_id'] != expected_id:
        raise SchemaError('event_id 必须是 %r' % expected_id)
    uptime = raw['uptime_ms']
    if not _is_int(uptime) or uptime < 0:
        raise SchemaError('uptime_ms 必须是 >= 0 的整数')
    if raw['clock_status'] not in CLOCK_STATUSES:
        raise SchemaError('clock_status 必须是 %s 之一' % '/'.join(CLOCK_STATUSES))
    if not isinstance(raw['firmware_version'], str) or not raw['firmware_version']:
        raise SchemaError('firmware_version 必须是非空字符串')

    rssi = raw.get('wifi_rssi_dbm')
    if rssi is not None and (not _is_int(rssi) or not -120 <= rssi <= 0):
        raise SchemaError('wifi_rssi_dbm 必须是 -120..0 的整数或 null')

    ip = raw.get('ip')
    if ip is not None and not isinstance(ip, str):
        raise SchemaError('ip 必须是字符串或 null')

    return {
        'schema_version': SCHEMA_VERSION,
        'device_id': device_id,
        'zone_id': zone_id,
        'boot_id': boot_id,
        'seq': seq,
        'event_id': expected_id,
        'uptime_ms': uptime,
        'clock_status': raw['clock_status'],
        'firmware_version': raw['firmware_version'],
        'wifi_rssi_dbm': rssi,
        'ip': ip,
    }


def effective_time_ms(event):
    """数据时间：设备对时后用它自己的采集时间，否则退回网关接收时间。

    网关接收时间只在存储层补上，绝不冒充设备采集时间。
    """
    return event.get('measured_at_ms') or event.get('received_at_ms')
