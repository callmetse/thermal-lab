"""YOLO pose -> ByteTrack -> timestamped per-person TCN candidates.

Writes incrementally. No LLM in frame loop, no numerical met from coarse actions.
Uses video PTS (or explicitly labeled index/FPS fallback), not compute duration.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT))
from ultralytics import YOLO
from infer import associate, mark_class_ambiguities
from person_selection import select_people
from clomet import estimate, visible_clothing_candidates
from action_model import ActionTCN
from temporal import CLASSES, PersonWindows


def iou(a, b):
    x1,y1,x2,y2 = a; u1,v1,u2,v2 = b
    inter = max(0, min(x2,u2)-max(x1,u1))*max(0, min(y2,v2)-max(y1,v1))
    union = max(0,x2-x1)*max(0,y2-y1)+max(0,u2-u1)*max(0,v2-v1)-inter
    return inter/union if union else 0.


class TemporalVision:
    def __init__(self, checkpoint=None, device='0', imgsz=416, max_people=0,
                 min_person_confidence=.25, min_person_height_ratio=0., pose_max_det=300,
                 action_batch=False):
        torch.set_num_threads(4)
        self.device = device
        self.imgsz = imgsz
        self.max_people = max_people
        self.min_person_confidence = min_person_confidence
        self.min_person_height_ratio = min_person_height_ratio
        self.pose_max_det = pose_max_det
        self.action_batch = bool(action_batch)
        self.last_selection = {}
        import os
        assets = Path(os.environ["ABC_ASSETS_DIR"]).resolve()
        self.pose_path = Path(os.environ.get("ABC_POSE_PATH", str(assets/"yolo11n-pose.pt")))
        self.pose = YOLO(str(self.pose_path))
        self.clothing_path = Path(os.environ.get("ABC_CLOTHING_PATH", str(assets/"clothing_best.pt")))
        self.clothes = YOLO(str(self.clothing_path))
        self.tables = json.loads((PROJECT/"reference_tables.json").read_text())
        self.clothing_cache = {}
        self.last_clothing_timestamp = -1e9
        self.windows = PersonWindows()
        self.action = None
        self.threshold = .95
        self.model_hash = None
        self.results = None
        if checkpoint:
            checkpoint = Path(checkpoint)
            ckpt = torch.load(checkpoint, map_location='cpu')
            self.action = ActionTCN().to('cuda:0' if device == '0' else device).eval()
            self.action.load_state_dict(ckpt['state_dict'])
            self.model_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            result_path = checkpoint.parent/'results.json'
            if result_path.exists():
                self.results = json.loads(result_path.read_text())
                self.threshold = self.results['candidate_confidence_threshold']
        self.sequence = 0
        self.last_timestamp = None

    def _action_probabilities(self, features_by_person):
        """Same FP32 model; batching avoids one GPU/CPU synchronization per person."""
        if self.action is None or not features_by_person:
            return {}
        with torch.inference_mode():
            if self.action_batch:
                indices = list(features_by_person)
                inputs = np.stack([features_by_person[i] for i in indices])
                x = torch.from_numpy(inputs).to(next(self.action.parameters()).device)
                probabilities = self.action(x).softmax(1).cpu().numpy()
                return dict(zip(indices, probabilities))
            probabilities = {}
            for i, feature in features_by_person.items():
                x = torch.from_numpy(feature[None]).to(next(self.action.parameters()).device)
                probabilities[i] = self.action(x).softmax(1)[0].cpu().numpy()
            return probabilities

    def frame(self, frame, timestamp):
        # ByteTrack owns ID association. This class owns temporal evidence quality.
        # Reset both after source-time reversal or a globally missing chunk.
        if self.last_timestamp is not None and (timestamp <= self.last_timestamp or timestamp-self.last_timestamp > .6):
            if hasattr(self.pose, 'predictor') and self.pose.predictor is not None:
                for tracker in getattr(self.pose.predictor, 'trackers', []):
                    tracker.reset()
            self.windows.clear()
            self.clothing_cache.clear()
            self.last_clothing_timestamp = -1e9
            self.sequence += 1
        self.last_timestamp = timestamp
        r = self.pose.track(frame, persist=True, tracker=str(ROOT/'bytetrack_temporal.yaml'),
                            conf=getattr(self,"pose_conf",.1), iou=.7, imgsz=self.imgsz, device=self.device,
                            max_det=self.pose_max_det, rect=False, verbose=False)[0]
        if r.boxes.id is None:
            self.last_selection = {'tracked_detections': 0, 'selected_people': 0}
            return [], r
        boxes = r.boxes.xyxy.cpu().numpy()
        ids = r.boxes.id.int().cpu().tolist()
        conf = r.boxes.conf.cpu().tolist()
        keypoints = r.keypoints.data.cpu().numpy()
        ambiguous = set()
        for i in range(len(boxes)):
            for j in range(i+1, len(boxes)):
                if iou(boxes[i], boxes[j]) > .65:
                    ambiguous.update([i, j])
        selected = select_people(boxes, conf, frame.shape[0], self.max_people,
                                 self.min_person_confidence, self.min_person_height_ratio)
        self.last_selection = {'tracked_detections': len(boxes), 'selected_people': len(selected)}
        histories = {}
        for i, track_id in enumerate(ids):
            if i in ambiguous:
                self.windows.drop(track_id)
                self.clothing_cache.pop(track_id, None)
                histories[i] = {'ready': False, 'reason': 'overlapping_tracks_identity_ambiguous'}
            else:
                histories[i] = self.windows.push(track_id, float(timestamp), keypoints[i], boxes[i])
                if histories[i].get('reason') in {'tracking_gap_reset', 'identity_geometry_jump_reset', 'nonmonotonic_timestamp_reset'}:
                    self.clothing_cache.pop(track_id, None)
        for track_id in list(self.clothing_cache):
            if timestamp-self.clothing_cache[track_id]['timestamp_s'] > 2.:
                self.clothing_cache.pop(track_id)
        # Clothing updates independently at 1 Hz. Never block every frame on the LLM.
        if timestamp-self.last_clothing_timestamp >= 1.:
            garments = self.clothes.predict(frame, imgsz=512, device=self.device, conf=.25, rect=False, verbose=False)[0]
            assigned = {i: [] for i in selected if i not in ambiguous}
            for box, cls, score in zip(garments.boxes.xyxy.cpu().tolist(), garments.boxes.cls.cpu().tolist(), garments.boxes.conf.cpu().tolist()):
                idx, reason = associate(box, boxes.tolist())
                if idx in assigned:
                    assigned[idx].append({'category': garments.names[int(cls)], 'bbox_xyxy': box,
                                          'confidence': score, 'observed_timestamp_s': timestamp})
            for i, items in assigned.items():
                mark_class_ambiguities(items)
                self.clothing_cache[ids[i]] = {'timestamp_s': timestamp, 'items': items}
            self.last_clothing_timestamp = timestamp
        action_probabilities = self._action_probabilities({
            i: histories[i]['features'] for i in selected
            if histories[i]['ready'] and self.action is not None})
        people = []
        for i in selected:
            history = histories[i]
            candidate = {'value': 'unknown', 'reason': history.get('reason') or 'action_checkpoint_missing'}
            if i in action_probabilities:
                prob = action_probabilities[i]
                j = int(prob.argmax())
                accepted = j != 0 and float(prob[j]) >= self.threshold
                candidate = {'value': CLASSES[j] if accepted else 'unknown',
                             'reason': 'offline_trained_candidate_field_validation_pending' if accepted else 'other_or_low_confidence',
                             'probabilities': dict(zip(CLASSES, map(float, prob))),
                             'confidence_threshold': self.threshold,
                             'model_sha256': self.model_hash}
            cached = self.clothing_cache.get(ids[i])
            clothes = cached['items'] if cached else []
            facts = {'observation_valid': True, 'observed_garments': clothes,
                     'clothing': {'complete': False, 'confirmed_ensemble': None},
                     'activity': {'context_verified': False, 'confirmed_task': None}}
            people.append({'person_id': 's%d_track_%d' % (self.sequence, ids[i]),
                           'tracking_id': ids[i], 'identity_scope': 'ephemeral_video_track',
                           'bbox_xyxy': boxes[i].tolist(), 'confidence': conf[i],
                           'keypoints_coco17': keypoints[i].tolist(),
                           'temporal_window': {k:v for k,v in history.items() if k != 'features'},
                           'activity_candidate': candidate,
                           'clothing': clothes,
                           'clothing_age_s': timestamp-cached['timestamp_s'] if cached else None,
                           'clothing_status': ('fresh' if timestamp == cached['timestamp_s'] else 'held') if cached else 'unknown',
                           'visible_clothing_reference': visible_clothing_candidates(clothes, self.tables),
                           'thermal_estimate': estimate(facts, self.tables),
                           'met': None, 'met_reason': 'activity_context_and_pace_not_validated'})
        return people, r


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--output', required=True)
    parser.add_argument('--checkpoint')
    parser.add_argument('--imgsz', type=int, default=416)
    parser.add_argument('--max-seconds', type=float, default=30)
    parser.add_argument('--device', default='0')
    args = parser.parse_args()
    source = Path(args.source)
    out = Path(args.output); out.mkdir(parents=True, exist_ok=False)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise ValueError('Cannot open video')
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError('Video lacks usable FPS; explicit timestamp input is required')
    w,h = int(cap.get(3)),int(cap.get(4))
    writer = cv2.VideoWriter(str(out/'tracking.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), fps, (w,h))
    pipeline = TemporalVision(args.checkpoint, args.device, args.imgsz)
    times = []; counts = {}; ready_count = 0; frames = 0; fallback_count = 0
    started = time.perf_counter(); last_pts = -1.
    with (out/'observations.jsonl').open('w') as log:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pts = float(cap.get(cv2.CAP_PROP_POS_MSEC))/1000
            basis = 'video_pts'
            if not np.isfinite(pts) or pts <= last_pts:
                pts = frames/fps; basis = 'frame_index_over_reported_fps'; fallback_count += 1
            if pts > args.max_seconds:
                break
            last_pts = pts
            begin = time.perf_counter()
            people, result = pipeline.frame(frame, pts)
            elapsed = time.perf_counter()-begin
            times.append(elapsed)
            canvas = frame.copy()
            for p in people:
                counts[p['person_id']] = counts.get(p['person_id'], 0)+1
                ready_count += int(p['temporal_window']['ready'])
                a,b,c,d = map(int, p['bbox_xyxy'])
                cv2.rectangle(canvas,(a,b),(c,d),(30,220,30),2)
                label = '%s %s %.2fs' % (p['person_id'], p['activity_candidate']['value'], p['temporal_window'].get('observation_span_s',0))
                cv2.putText(canvas,label,(a,max(18,b-5)),cv2.FONT_HERSHEY_SIMPLEX,.5,(30,220,30),1)
                for x,y,score in p['keypoints_coco17']:
                    if score >= .3:
                        cv2.circle(canvas,(int(x),int(y)),2,(0,255,255),-1)
            writer.write(canvas)
            log.write(json.dumps({'frame': frames, 'timestamp_s': pts, 'time_basis': basis,
                                  'produced_unix': time.time(), 'source_is_offline': True,
                                  'captured_at_utc': None, 'selection': {'max_people': pipeline.max_people,
                                  'min_person_confidence': pipeline.min_person_confidence,
                                  'min_person_height_ratio': pipeline.min_person_height_ratio},
                                  'vision_action_seconds': elapsed, 'people': people})+'\n')
            frames += 1
            if frames % 100 == 0:
                log.flush(); print('FRAMES', frames, flush=True)
    cap.release(); writer.release()
    summary = {'frames': frames, 'source_fps': fps, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
               'track_observation_counts': counts, 'ready_person_observations': ready_count,
               'timestamp_fallback_frames': fallback_count, 'wall_seconds': time.perf_counter()-started,
               'vision_action_median_ms': float(np.median(times)*1000) if times else None,
               'vision_action_p95_ms': float(np.percentile(times,95)*1000) if times else None,
               'pose_sha256': hashlib.sha256(pipeline.pose_path.read_bytes()).hexdigest(),
               'clothing_sha256': hashlib.sha256(pipeline.clothing_path.read_bytes()).hexdigest(),
               'action_sha256': pipeline.model_hash,
               'tracking_accuracy': 'unmeasured_no_identity_ground_truth',
               'activity_accuracy': 'unmeasured_no_temporal_ground_truth_for_demo',
               'met': None, 'status': 'real_video_integration_smoke_not_accuracy_validation'}
    (out/'summary.json').write_text(json.dumps(summary, indent=2))
    print('TEMPORAL_VIDEO_OK', json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
