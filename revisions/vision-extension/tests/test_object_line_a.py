"""A 线路（仅物体检测）的纯逻辑单测：不加载模型、不连 MQTT。

覆盖目标里可离线验证的三件事：
1. 每个轨迹 ≤2 Hz 才上传裁剪；
2. 画面没明显变化就不重复上传；
3. 云端回来的属性会挂回同一 track。
另有慢帧跳过与拼图槽位布局。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab.object_line import ObjectLine  # noqa: E402


class _Array:
    def __init__(self, value):
        self._value = np.asarray(value)

    def cpu(self):
        return self

    def numpy(self):
        return self._value

    def tolist(self):
        return self._value.tolist()

    def int(self):
        return _Array(self._value.astype(int))


class _Boxes:
    def __init__(self, boxes):
        self.xyxy = _Array([b[:4] for b in boxes])
        self.id = _Array([int(b[4]) for b in boxes])
        self.conf = _Array([float(b[5]) for b in boxes])
        self.cls = _Array([int(b[6]) for b in boxes])

    def __len__(self):
        return len(self.xyxy.tolist())


class _Result:
    names = {0: "person", 2: "car"}

    def __init__(self, boxes):
        self.boxes = _Boxes(boxes) if boxes else None


class FakeModel:
    """按顺序吐出预设检测结果。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    def track(self, image, **kwargs):
        self.calls.append(kwargs)
        boxes = self.frames.pop(0) if self.frames else []
        return [_Result(boxes)]


def make_line(config_overrides, frames, attrs=None):
    config = {"a_crop_side": 64, "a_tile_slots": 4, "a_max_hz": 2,
              "a_change_threshold": 8.0, "a_imgsz": 416, "pose_max_det": 300,
              "jpeg_quality": 80, "a_skip_crops_over_ms": 450}
    config.update(config_overrides)
    published = []
    line = ObjectLine(config, lambda run, meta, jpeg: published.append((meta, jpeg)), attrs=attrs)
    line.model = FakeModel(frames)
    return line, published


def frame(seed=0):
    image = np.zeros((360, 640, 3), dtype=np.uint8)
    image[40 + seed * 20:160 + seed * 20, 100:220] = 200
    return image


RUN = {"run_id": "unit-a", "arm": "A", "source": "replay", "confidence_min": 0.25,
       "confidence_max": 1.0, "recognition_region": {"mode": "full"}, "object_classes": [0]}
HEADER = {"project": "abc20261102", "run_id": "unit-a", "arm": "A", "source": "replay",
          "source_frame_id": 0, "source_pts_s": 0.0, "source_sha256": "0" * 64}


def test_rate_limit_and_change_gate():
    boxes = [[100, 40, 220, 160, 7, 0.9, 0]]
    line, pub = make_line({}, [boxes, boxes, boxes, boxes])
    line.infer(frame(0), 0.0, RUN, HEADER)      # 新轨迹 → 必传
    line.infer(frame(0), 0.1, RUN, HEADER)      # 0.5s 内 → 不传
    line.infer(frame(0), 0.2, RUN, HEADER)      # 仍不足 0.5s → 不传
    assert len(pub) == 1, len(pub)

    line.infer(frame(3), 0.6, RUN, HEADER)      # 超过 0.5s 且画面变了 → 传
    assert len(pub) == 2, len(pub)

    line2, pub2 = make_line({}, [boxes, boxes])
    line2.infer(frame(0), 0.0, RUN, HEADER)
    line2.infer(frame(0), 1.0, RUN, HEADER)     # 过了 2 Hz，但画面没变 → 仍不传
    assert len(pub2) == 1, len(pub2)


def test_tile_layout_and_meta():
    boxes = [[100, 40, 220, 160, 7, 0.9, 0],
             [300, 40, 420, 160, 8, 0.8, 0],
             [100, 200, 220, 320, 9, 0.7, 0]]
    line, pub = make_line({"a_tile_slots": 9, "a_crop_side": 32}, [boxes])
    tracks, _ = line.infer(frame(0), 0.0, RUN, HEADER)
    assert len(tracks) == 3
    assert len(pub) == 1
    meta, jpeg = pub[0]
    assert meta["slot_side"] == 32 and len(meta["slots"]) == 3
    rows = max(s["row"] for s in meta["slots"]) + 1
    cols = max(s["col"] for s in meta["slots"]) + 1
    assert meta["tile_size"] == [cols * 32, rows * 32], meta["tile_size"]
    assert len(jpeg) > 100 and jpeg[:2] == b"\xff\xd8"   # JPEG 头
    assert meta["slots"][0]["class_name"] == "person"


def test_attributes_merge_and_slow_guard():
    boxes = [[100, 40, 220, 160, 7, 0.9, 0]]
    cached = {"posture": {"state": "standing", "reason": None},
              "clothing": [{"category": "pants"}], "class_name": "person"}
    line, pub = make_line({}, [boxes], attrs=lambda track_id: cached if track_id == 7 else None)
    tracks, _ = line.infer(frame(0), 0.0, RUN, HEADER)
    assert tracks[0]["posture"]["state"] == "standing"
    assert tracks[0]["clothing"][0]["category"] == "pants"
    assert tracks[0]["attributes"] is cached

    slow, pub_slow = make_line({"a_skip_crops_over_ms": 0.0}, [boxes])
    slow.infer(frame(0), 0.0, RUN, HEADER)
    assert pub_slow == []            # 慢帧保护：超时不上传
    assert slow.skipped_slow == 1


def test_class_filter_passthrough():
    boxes = [[100, 40, 220, 160, 7, 0.9, 2]]
    line, pub = make_line({}, [boxes, boxes])
    run = dict(RUN, object_classes=[0, 2])
    tracks, _ = line.infer(frame(0), 0.0, run, HEADER)
    assert line.model.calls[0]["classes"] == [0, 2]
    assert tracks[0]["class_name"] == "car"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
