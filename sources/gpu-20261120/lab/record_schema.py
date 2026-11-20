"""记录 Schema 与运行时校验（P2-10）。

归档写入之前先校验，非法记录进 `experiment_errors`（隔离），不再静默收下：

- observation：必须带 `producer_instance_id` / `observation_seq` / `tracks`；
  每个 track 的 `bbox` 必须是 4 个有限数、**x2≥x1、y2≥y1、非负**；`confidence` 必须在 0..1。
  坐标单位：真实记录是**像素**（中心/角点都按整帧像素存），所以默认按像素校验并设 16384 上限；
  若记录显式声明 `bbox_space="normalized"`，则收紧为 0..1。审查原文写的是"坐标在 0..1"，
  这里按实际数据形态落地为"有限、有序、非负、不越界"，两种单位都拒绝负值与倒序。
- event：必须有非空 `kind`。
- environment：必须有 `sensor_id` 与 `values`（dict）。

有 `jsonschema` 就用它（Pi 装了 4.19.2）；没有就用同一套规则的内置校验，两条路径行为一致
（测试里会交叉验证）。
"""
import math

try:  # pragma: no cover - 取决于环境
    import jsonschema
    HAS_JSONSCHEMA = True
except Exception:  # noqa: BLE001 - 没装就退回内置校验
    jsonschema = None
    HAS_JSONSCHEMA = False

MAX_COORD = 16384.0
MAX_TRACKS = 512

OBSERVATION = {
    "type": "object",
    "required": ["producer_instance_id", "observation_seq", "tracks"],
    "properties": {
        "producer_instance_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "observation_seq": {"type": "integer", "minimum": 0},
        "source_frame_id": {"type": "integer", "minimum": 0},
        "tracks": {"type": "array", "maxItems": MAX_TRACKS, "items": {
            "type": "object",
            "required": ["bbox", "confidence"],
            "properties": {
                "bbox": {"type": "array", "minItems": 4, "maxItems": 4,
                         "items": {"type": "number"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "track_id": {"type": "integer"},
                "class_id": {"type": "integer"},
                "class_name": {"type": "string"},
                "bbox_space": {"enum": ["pixels", "normalized"]},
            },
        }},
    },
}

EVENT = {
    "type": "object",
    "required": ["kind"],
    "properties": {"kind": {"type": "string", "minLength": 1, "maxLength": 80}},
}

ENVIRONMENT = {
    "type": "object",
    "required": ["sensor_id", "values"],
    "properties": {"sensor_id": {"type": "string", "minLength": 1},
                   "values": {"type": "object"},
                   "quality": {"type": "string"}},
}

SCHEMAS = {"observation": OBSERVATION, "event": EVENT, "environment": ENVIRONMENT}
TITLES = {"observation": "Experiment observation payload v1",
          "event": "Experiment event payload v1",
          "environment": "Environment record payload v1"}


class RecordInvalid(ValueError):
    """记录不满足 Schema：调用方应把它隔离到 experiment_errors，而不是写进正式记录。"""


_VALIDATORS = {}


def _validator(kind):
    """缓存编译好的校验器：每调一次 jsonschema.validate 都会重编译，写线程吃不消。"""
    if not HAS_JSONSCHEMA:
        return None
    if kind not in _VALIDATORS:
        _VALIDATORS[kind] = jsonschema.Draft202012Validator(SCHEMAS[kind])
    return _VALIDATORS[kind]


def _type_ok(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return True


def _check(value, schema, path):
    """内置校验：只覆盖本文件用到的关键字，规则与 jsonschema 版本一致。"""
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        raise RecordInvalid("%s: 期望 %s，实际 %s" % (path, expected, type(value).__name__))
    if "enum" in schema and value not in schema["enum"]:
        raise RecordInvalid("%s: 取值不在 %s 内" % (path, schema["enum"]))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise RecordInvalid("%s: 字符串太短" % path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise RecordInvalid("%s: 字符串超长" % path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise RecordInvalid("%s: %s < 最小值 %s" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            raise RecordInvalid("%s: %s > 最大值 %s" % (path, value, schema["maximum"]))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise RecordInvalid("%s: 元素太少（%d）" % (path, len(value)))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise RecordInvalid("%s: 元素太多（%d）" % (path, len(value)))
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _check(item, item_schema, "%s[%d]" % (path, index))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise RecordInvalid("%s: 缺少必需字段 %s" % (path, key))
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                _check(value[key], sub, "%s.%s" % (path, key))


def _cross_checks(kind, body):
    if kind != "observation":
        return
    for index, track in enumerate(body.get("tracks") or []):
        bbox = track.get("bbox") or []
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox):
            raise RecordInvalid("tracks[%d].bbox: 必须全是有限数" % index)
        x1, y1, x2, y2 = bbox
        space = track.get("bbox_space", "pixels")
        limit = 1.0 if space == "normalized" else MAX_COORD
        if min(bbox) < 0:
            raise RecordInvalid("tracks[%d].bbox: 存在负坐标 %s" % (index, bbox))
        if max(bbox) > limit:
            raise RecordInvalid("tracks[%d].bbox: 坐标 %s 超出 %s 上限 %s"
                                % (index, bbox, space, limit))
        if x2 < x1 or y2 < y1:
            raise RecordInvalid("tracks[%d].bbox: 坐标倒序 [%s, %s, %s, %s]" % (index, x1, y1, x2, y2))


def validate(kind, body):
    schema = SCHEMAS.get(kind)
    if schema is None:
        return  # 未知记录类型放行，保证前向兼容
    if HAS_JSONSCHEMA:
        validator = _validator(kind)
        errors = sorted(validator.iter_errors(body), key=lambda item: list(item.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            raise RecordInvalid("%s: %s" % (location, error.message))
    else:
        _check(body, schema, kind)
    _cross_checks(kind, body)


def schema_bundle():
    """导出包里的 record_schema.json：信封 + 三种 payload 的约束。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Experiment record envelope v1",
        "type": "object",
        "required": ["schema_version", "batch_id", "record_type", "payload"],
        "properties": {
            "schema_version": {"const": "1.0"},
            "batch_id": {"type": "string"},
            "record_type": {"enum": ["observation", "event", "environment"]},
            "payload": {"type": "object"},
            "record_id": {"type": "string"},
            "received_at_ms": {"type": "integer"},
            "review_status": {"type": "string"},
        },
        "additionalProperties": True,
        "$defs": {"observation": OBSERVATION, "event": EVENT, "environment": ENVIRONMENT},
        "x-validation": {"engine": "jsonschema" if HAS_JSONSCHEMA else "builtin",
                         "max_coord": MAX_COORD, "max_tracks": MAX_TRACKS,
                         "bbox_order": "x2>=x1 and y2>=y1",
                         "bbox_space": "pixels by default, normalized when declared"},
    }
