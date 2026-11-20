"""云端：消费 A 线路的裁剪拼图，对人物回姿态/服饰，对其它物体只回类别。

裁剪来自 Pi，已经在框内放大过，所以这里对人物只跑一次姿态 + 一次服饰，
不重复做整帧检测；非人物类别由 Pi 的检测器给出，直接回传。
"""
import time

import cv2
import numpy as np

from .common import ROOT
from .posture import estimate_posture


class CropInfer:
    def __init__(self, c):
        self.c = c
        self.pose = None
        self.clothes = None
        self.garment_names = None

    def load(self):
        from ultralytics import YOLO
        self.pose = YOLO(str(ROOT / 'assets/yolo11n-pose.pt'))
        self.clothes = YOLO(str(ROOT / 'assets/clothing_best.pt'))

    def handle(self, meta, tile_jpeg, publish):
        if self.pose is None:
            self.load()
        slot_side = int(meta.get('slot_side', 320))
        buffer = np.frombuffer(tile_jpeg, dtype=np.uint8)
        tile = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if tile is None:
            return 0
        start = time.perf_counter_ns()
        tracks = []
        for slot in meta.get('slots', []):
            row, col = int(slot.get('row', 0)), int(slot.get('col', 0))
            crop = tile[row * slot_side:(row + 1) * slot_side, col * slot_side:(col + 1) * slot_side]
            if crop.size == 0:
                continue
            attributes = {'class_name': slot.get('class_name'), 'class_id': slot.get('class_id'),
                          'confidence': slot.get('confidence')}
            if slot.get('class_name') == 'person':
                attributes.update(self._person(crop))
            tracks.append({'track_id': slot.get('track_id'), 'bbox': slot.get('bbox'),
                           'source_confidence': slot.get('confidence'),
                           'attributes': attributes})
        infer_ms = (time.perf_counter_ns() - start) / 1e6
        publish(meta, tracks, infer_ms)
        return len(tracks)

    def _person(self, crop):
        out = {'posture': {'state': 'unknown', 'reason': 'crop_inference_failed'},
               'clothing': [], 'garments_age_s': 0.0}
        pose_start = time.perf_counter_ns()
        pose = self.pose.predict(crop, imgsz=int(self.c.get('a_crop_pose_imgsz', 640)),
                                 conf=.1, iou=.7, device=self.c.get('a_crop_device', '0'),
                                 rect=False, verbose=False, max_det=1)[0]
        out['pose_ms'] = (time.perf_counter_ns() - pose_start) / 1e6
        if pose.boxes is None or len(pose.boxes) == 0:
            out['posture'] = {'state': 'unknown', 'reason': 'no_person_in_crop'}
            return out
        index = int(pose.boxes.conf.argmax())
        box = pose.boxes.xyxy[index].cpu().tolist()
        keypoints = pose.keypoints.data[index].cpu().numpy()
        out['posture'] = estimate_posture({'keypoints_coco17': keypoints.tolist()}, box[3] - box[1])
        out['crop_confidence'] = float(pose.boxes.conf[index].cpu().item())
        garments = self.clothes.predict(crop, imgsz=int(self.c.get('a_crop_clothing_imgsz', 512)),
                                        conf=.25, device=self.c.get('a_crop_device', '0'),
                                        rect=False, verbose=False)[0]
        items = []
        if garments.boxes is not None:
            for b, cls, score in zip(garments.boxes.xyxy.cpu().tolist(),
                                     garments.boxes.cls.cpu().tolist(),
                                     garments.boxes.conf.cpu().tolist()):
                items.append({'category': garments.names[int(cls)], 'bbox_xyxy': b,
                              'confidence': score, 'observed_timestamp_s': 0.0})
        out['clothing'] = items
        return out
