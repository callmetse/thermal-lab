"""Adapters around existing weights, Ultralytics tracking and copied TCN code."""
import os
import sys
import time

from .common import ROOT
from .posture import estimate_posture


class Vision:
    def __init__(self, c, edge):
        if c.get("rect") is not False:
            raise ValueError("Use identical square preprocessing for the NCNN/PyTorch comparison")
        assets = ROOT / "assets"
        os.environ["ABC_ASSETS_DIR"] = str(assets)
        suffix = "_ncnn_model" if edge else ".pt"
        os.environ["ABC_POSE_PATH"] = str(assets / ("yolo11n-pose" + suffix))
        os.environ["ABC_CLOTHING_PATH"] = str(assets / ("clothing_best" + suffix))
        algorithm = ROOT / "vendor/algorithm"
        sys.path.insert(0, str(algorithm))
        sys.path.insert(0, str(algorithm / "temporal_v1"))
        from track_video import TemporalVision
        self.pipeline = TemporalVision(checkpoint=assets / "action/best.pt",
                                       device="cpu" if edge else "0", imgsz=c["pose_imgsz"],
                                       max_people=c["max_people"],
                                       min_person_confidence=c["min_person_confidence"],
                                       min_person_height_ratio=c["min_person_height_ratio"],
                                       pose_max_det=c["pose_max_det"],
                                       action_batch=not edge and c.get("cloud_action_batch", False))

    def reset(self):
        p = self.pipeline
        p.windows.clear()
        p.clothing_cache.clear()
        p.last_clothing_timestamp = -1e9
        p.last_timestamp = None
        if getattr(p.pose, "predictor", None):
            for tracker in getattr(p.pose.predictor, "trackers", []):
                tracker.reset()

    def configure_run(self, run):
        from .recognition_region import RecognitionRegion
        self.region=RecognitionRegion(run)
        self.pipeline.min_person_confidence=self.region.low
        self.pipeline.pose_conf=max(.001,min(.1,self.region.low))

    def infer(self, image, source_s):
        start = time.perf_counter_ns()
        people, _ = self.pipeline.frame(image, source_s)
        tracks = []
        for p in people:
            points = p.get("keypoints_coco17", [])
            visible = len(points) == 17 and all(points[i][2] >= .3 for i in (5, 6, 11, 12, 13, 14, 15, 16))
            posture = estimate_posture(p, p["bbox_xyxy"][3] - p["bbox_xyxy"][1]) if visible else {
                "state": "unknown", "reason": "lower_body_or_torso_not_visible"}
            win = p["temporal_window"]
            candidate = p["activity_candidate"]
            tracks.append({
                "track_id": p["tracking_id"], "bbox": p["bbox_xyxy"], "confidence": p.get("confidence"),
                "posture": posture,
                "action": candidate if win.get("ready") else {"value": "unknown", "reason": "insufficient_sampling"},
                "window": win, "clothing": p["clothing"],
                "clothing_age_s": p.get("clothing_age_s"),
                "clo": None, "met": None,
                "estimate_status": "unknown_unconfirmed_ensemble_and_activity",
                "visible_clo_reference": p["visible_clothing_reference"],
            })
        region=getattr(self,'region',None)
        if region is not None:
            tracks,events,counts=region.apply(tracks,(image.shape[1],image.shape[0]),source_s)
            self.last_region_evidence={'recognition_region':region.region,'confidence_range':[region.low,region.high],
                                      'crossing_events':events,'crossing_counts':counts}
        return tracks, (time.perf_counter_ns() - start) / 1e6


def observation(header, tracks, infer_ms, producer, timing=None):
    return {**header, "kind": "observation", "producer": producer,
            "status": "valid_observation" if tracks else "model_empty_candidate",
            "tracks": tracks, "infer_ms": infer_ms, "timing": timing or {},
            "cross_machine_age_ms": None, "clock_status": "not_validated"}
