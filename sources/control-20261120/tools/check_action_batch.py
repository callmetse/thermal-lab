"""Check serial/batched TCN execution on real unlabelled video windows, never accuracy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.common import config

import cv2
import numpy as np
import torch

ALGORITHM = ROOT / 'vendor/algorithm'
sys.path.insert(0, str(ALGORITHM))
sys.path.insert(0, str(ALGORITHM / 'temporal_v1'))
from track_video import TemporalVision
from temporal import features, resample


def synchronize(device):
    if device.type == 'cuda':
        torch.cuda.synchronize(device)


def decisions(probabilities, threshold):
    return [int(row.argmax()) if int(row.argmax()) != 0 and float(row.max()) >= threshold else 0
            for row in probabilities]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'data/replay.mp4')
    parser.add_argument('--device', choices=['0', 'cpu'], default='0')
    parser.add_argument('--seconds', type=float, default=4)
    parser.add_argument('--groups', type=int, default=5)
    parser.add_argument('--min-people', type=int, default=2)
    parser.add_argument('--repeats', type=int, default=20)
    parser.add_argument('--output', type=Path, default=ROOT / 'results/action-batch-check.json')
    args = parser.parse_args()
    if args.device == '0' and not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable; use --device cpu explicitly for a CPU-only check')
    c = config()
    for variable, name in [('ABC_ASSETS_DIR', ''), ('ABC_POSE_PATH', 'yolo11n-pose.pt'),
                           ('ABC_CLOTHING_PATH', 'clothing_best.pt')]:
        os.environ[variable] = str(ROOT / 'assets' / name)
    pipeline = TemporalVision(ROOT / 'assets/action/best.pt', device=args.device,
                              imgsz=c['pose_imgsz'], max_people=c['max_people'],
                              min_person_confidence=c['min_person_confidence'],
                              min_person_height_ratio=c['min_person_height_ratio'],
                              pose_max_det=c['pose_max_det'], action_batch=False)
    cap = cv2.VideoCapture(str(args.source))
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not cap.isOpened() or fps <= 0:
        raise RuntimeError('Cannot read a video with known FPS')
    groups = []
    try:
        for index in range(int(args.seconds * fps)):
            ok, image = cap.read()
            if not ok:
                break
            if max(image.shape[:2]) > c['encode_max_side']:
                scale = c['encode_max_side'] / max(image.shape[:2])
                image = cv2.resize(image, (round(image.shape[1] * scale), round(image.shape[0] * scale)),
                                   interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, c['jpeg_quality']])
            if not ok:
                raise RuntimeError('JPEG preparation failed')
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            people, _ = pipeline.frame(image, index / fps)
            ready = [p for p in people if p['temporal_window']['ready']]
            if len(ready) < args.min_people:
                continue
            group = {}
            for person in ready:
                history = pipeline.windows.tracks[person['tracking_id']]
                # Recreate the exact feature function used by PersonWindows.push.
                group[person['tracking_id']] = features(resample(
                    [h[0] for h in history], [h[1] for h in history]))
            groups.append({'source_frame_id': index, 'source_pts_s': index / fps, 'features': group})
            if len(groups) >= args.groups:
                break
    finally:
        cap.release()
    if not groups:
        raise RuntimeError('No real multi-person ready windows; do not substitute random test data')
    device = next(pipeline.action.parameters()).device
    output = []
    fixtures = {}
    all_equal = True
    for number, group in enumerate(groups):
        inputs = group['features']
        indices = list(inputs)
        array = np.stack([inputs[i] for i in indices])
        fixtures['group_%03d' % number] = array
        with torch.inference_mode():
            serial_logits = torch.cat([pipeline.action(torch.from_numpy(inputs[i][None]).to(device))
                                       for i in indices]).cpu().numpy()
            batch_logits = pipeline.action(torch.from_numpy(array).to(device)).cpu().numpy()
        probabilities = {}
        for batch in [False, True]:
            pipeline.action_batch = batch
            result = pipeline._action_probabilities(inputs)
            probabilities[batch] = np.stack([result[i] for i in indices])
        logits_equal = bool(np.allclose(serial_logits, batch_logits, atol=1e-5, rtol=1e-5))
        probs_equal = bool(np.allclose(probabilities[False], probabilities[True], atol=1e-6, rtol=1e-5))
        decision_equal = decisions(probabilities[False], pipeline.threshold) == decisions(probabilities[True], pipeline.threshold)
        timings = {False: [], True: []}
        for repeat in range(args.repeats + 5):
            for batch in ([False, True] if repeat % 2 == 0 else [True, False]):
                pipeline.action_batch = batch
                synchronize(device)
                start = time.perf_counter()
                pipeline._action_probabilities(inputs)
                synchronize(device)
                if repeat >= 5:
                    timings[batch].append((time.perf_counter() - start) * 1000)
        all_equal = all_equal and logits_equal and probs_equal and decision_equal
        output.append({'source_frame_id': group['source_frame_id'], 'source_pts_s': group['source_pts_s'],
                       'track_ids': indices, 'people': len(inputs),
                       'max_abs_logit_difference': float(np.max(np.abs(serial_logits - batch_logits))),
                       'max_abs_probability_difference': float(np.max(np.abs(probabilities[False] - probabilities[True]))),
                       'logits_allclose': logits_equal, 'probabilities_allclose': probs_equal,
                       'thresholded_candidates_equal': decision_equal,
                       'serial_p50_ms': float(np.median(timings[False])),
                       'serial_p95_ms': float(np.percentile(timings[False], 95)),
                       'batch_p50_ms': float(np.median(timings[True])),
                       'batch_p95_ms': float(np.percentile(timings[True], 95))})
    result = {'scope': 'Numeric/backend agreement and TCN-only timing on real unlabelled video features; NOT action accuracy or end-to-end FPS',
              'source': str(args.source), 'source_sha256': hashlib.sha256(args.source.read_bytes()).hexdigest(),
              'action_checkpoint_sha256': pipeline.model_hash, 'torch': torch.__version__, 'device': str(device),
              'precision': str(next(pipeline.action.parameters()).dtype), 'threshold': pipeline.threshold,
              'groups': output, 'all_numeric_and_decision_checks_passed': all_equal,
              'timing_scope': 'TCN helper including CPU stack and device transfers; concurrent GPU load can affect these timings',
              'physical_accuracy': 'unmeasured'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    np.savez_compressed(args.output.with_suffix('.features.npz'), **fixtures)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not all_equal:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
