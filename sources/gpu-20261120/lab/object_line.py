"""A 线路 v2：本机只做物体检测，框内裁剪按需上云做属性推理。

设计约束（与现有三臂保持一致）：
- 帧来源、采样节拍、区域/置信度语义都复用既有实现，不另起一套。
- 上云只传"裁剪拼图"，不传整帧；同一轨迹最快 a_max_hz 上传一次，且画面没明显变化就不传。
- 观测先由本机发出（框立刻可见），云端属性作为第二条观测回填，互不阻塞。
"""
import os
import time
from pathlib import Path

import cv2
import numpy as np

from .common import ROOT


def _thumb(image, side=32):
    small = cv2.resize(image, (side, side), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


class ObjectLine:
    def __init__(self, c, publish, event=None, attrs=None):
        self.c = c
        self.publish = publish
        self.event = event
        self.attrs = attrs
        self.model = None
        self.state = {}
        self.sequence = 0
        self.slot_side = int(c.get('a_crop_side', 320))
        self.max_slots = max(1, min(16, int(c.get('a_tile_slots', 9))))
        self.max_hz = max(.1, float(c.get('a_max_hz', 2)))
        self.change_threshold = float(c.get('a_change_threshold', 8.0))

    def reset(self):
        """每个会话重新开始编号：状态清空，跟踪器也重置。"""
        self.state.clear()
        predictor = getattr(self.model, 'predictor', None)
        for tracker in getattr(predictor, 'trackers', []) or []:
            try:
                tracker.reset()
            except Exception:  # noqa: BLE001 - 旧版 tracker 没有 reset 时忽略
                pass

    def load(self):
        from ultralytics import YOLO
        override = os.environ.get('ABC_A_MODEL') or self.c.get('a_model')
        path = Path(override) if override else (ROOT / 'assets/yolo11n_object_ncnn_model')
        if not Path(path).exists():
            raise RuntimeError('先用 assets/yolo11n.pt 导出 640 的 NCNN 到 assets/yolo11n_object_ncnn_model')
        self.model = YOLO(str(path))

    # ---------- 裁剪与拼图 ----------
    def _crop(self, image, box, pad=.08):
        h, w = image.shape[:2]
        x1, y1, x2, y2 = [float(v) for v in box[:4]]
        bw, bh = max(1., x2 - x1), max(1., y2 - y1)
        x1, y1 = x1 - bw * pad, y1 - bh * pad
        x2, y2 = x2 + bw * pad, y2 + bh * pad
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(w, int(round(x2))), min(h, int(round(y2)))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = image[y1:y2, x1:x2]
        side = self.slot_side
        scale = min(side / crop.shape[1], side / crop.shape[0])
        resized = cv2.resize(crop, (max(1, int(round(crop.shape[1] * scale))),
                                    max(1, int(round(crop.shape[0] * scale)))),
                             interpolation=cv2.INTER_AREA)
        canvas = np.zeros((side, side, 3), dtype=np.uint8)
        top = (side - resized.shape[0]) // 2
        left = (side - resized.shape[1]) // 2
        canvas[top:top + resized.shape[0], left:left + resized.shape[1]] = resized
        return canvas

    def _tile(self, crops):
        count = len(crops)
        cols = int(np.ceil(np.sqrt(count)))
        rows = int(np.ceil(count / cols))
        side = self.slot_side
        tile = np.zeros((rows * side, cols * side, 3), dtype=np.uint8)
        for index, crop in enumerate(crops):
            row, col = divmod(index, cols)
            tile[row * side:(row + 1) * side, col * side:(col + 1) * side] = crop
        return tile, cols, rows

    # ---------- 主推理 ----------
    def infer(self, image, timestamp_s, run, header):
        if self.model is None:
            self.load()
        from .recognition_region import settings
        low, high, region = settings(run)
        classes = run.get('object_classes') or self.c.get('a_classes') or [0]
        classes = [int(v) for v in classes if isinstance(v, int) or str(v).isdigit()]
        start = time.perf_counter_ns()
        result = self.model.track(
            image, persist=True, tracker=str(ROOT / 'deploy/a_line_bytetrack.yaml'),
            conf=max(.001, min(.1, low)), iou=.7, imgsz=int(self.c.get('a_imgsz', 640)),
            device=str(self.c.get('a_device', 'cpu')), max_det=int(self.c.get('pose_max_det', 300)),
            classes=classes, rect=False, verbose=False)[0]
        infer_ms = (time.perf_counter_ns() - start) / 1e6
        self.last_infer_ms = infer_ms
        if result.boxes is None or result.boxes.id is None:
            self._expire(timestamp_s)
            return [], infer_ms
        names = result.names
        boxes = result.boxes.xyxy.cpu().numpy()
        ids = result.boxes.id.int().cpu().tolist()
        scores = result.boxes.conf.cpu().tolist()
        labels = result.boxes.cls.int().cpu().tolist()
        height, width = image.shape[:2]
        tracks, pending = [], []
        for i, track_id in enumerate(ids):
            score = scores[i]
            if not low <= score <= high:
                continue
            box = boxes[i].tolist()
            if region['mode'] == 'rectangle':
                (rx1, ry1), (rx2, ry2) = region['points']
                cx, cy = (box[0] + box[2]) / 2 / width, (box[1] + box[3]) / 2 / height
                if not (rx1 <= cx <= rx2 and ry1 <= cy <= ry2):
                    continue
            class_id = labels[i]
            cloud = self.attrs(track_id) if self.attrs else None
            tracks.append({'track_id': track_id, 'bbox': box, 'confidence': score,
                           'class_id': class_id, 'class_name': names[class_id],
                           'attributes': cloud,
                           'identity_scope': 'ephemeral_video_track',
                           'posture': (cloud or {}).get('posture') or
                                      {'state': 'pending_cloud', 'reason': 'a_line_crop_pending'},
                           'activity_candidate': (cloud or {}).get('activity_candidate') or
                                                 {'value': 'pending_cloud', 'reason': 'a_line_crop_pending'},
                           'clothing': (cloud or {}).get('clothing') or []})
            crop = self._crop(image, box)
            if crop is None:
                continue
            state = self.state.get(track_id)
            if self._should_send(state, crop, timestamp_s):
                pending.append({'track_id': track_id, 'bbox': box, 'class_id': class_id,
                                'class_name': names[class_id], 'confidence': score, 'crop': crop})
        self._expire(timestamp_s, alive=set(ids))
        slow_ms = float(self.c.get('a_skip_crops_over_ms', 450))
        if pending and infer_ms <= slow_ms:
            self._emit(run, header, pending, (width, height), timestamp_s)
        elif pending:
            self.skipped_slow = getattr(self, 'skipped_slow', 0) + 1
        return tracks, infer_ms

    def _should_send(self, state, crop, timestamp_s):
        if state is None:
            return True
        if timestamp_s - state['last_send'] < 1. / self.max_hz:
            return False
        diff = float(np.abs(_thumb(crop) - state['thumb']).mean())
        return diff >= self.change_threshold

    def _emit(self, run, header, pending, frame_size, timestamp_s):
        pending.sort(key=lambda item: (item['bbox'][2] - item['bbox'][0]) * (item['bbox'][3] - item['bbox'][1]),
                     reverse=True)
        pending = pending[:self.max_slots]
        tile, cols, rows = self._tile([item['crop'] for item in pending])
        ok, buf = cv2.imencode('.jpg', tile, [cv2.IMWRITE_JPEG_QUALITY, int(self.c.get('jpeg_quality', 80))])
        if not ok:
            return
        slots = []
        for index, item in enumerate(pending):
            row, col = divmod(index, cols)
            slots.append({'slot': index, 'track_id': item['track_id'], 'bbox': item['bbox'],
                          'class_id': item['class_id'], 'class_name': item['class_name'],
                          'confidence': item['confidence'], 'row': row, 'col': col})
            self.state[item['track_id']] = {'last_send': timestamp_s, 'thumb': _thumb(item['crop'])}
        meta = {**{k: header[k] for k in ('project', 'run_id', 'arm', 'source', 'source_frame_id',
                                          'source_pts_s', 'source_sha256')},
                'kind': 'crop_batch', 'slots': slots, 'slot_side': self.slot_side,
                'tile_size': [tile.shape[1], tile.shape[0]], 'frame_size': list(frame_size),
                'tile_sha256': __import__('hashlib').sha256(buf.tobytes()).hexdigest()}
        self.publish(run, meta, buf.tobytes())

    def _expire(self, timestamp_s, alive=None):
        alive = alive or set()
        for track_id in list(self.state):
            if track_id not in alive and timestamp_s - self.state[track_id]['last_send'] > 3.:
                self.state.pop(track_id, None)
