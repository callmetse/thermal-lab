"""Timestamped COCO17 buffers. Padding and missed observations stay explicitly masked."""
from collections import deque
import math
import numpy as np

SLOTS = 16
HZ = 8.0
CLASSES = ['other', 'sit_down', 'stand_up', 'walking_interaction']


def resample(times, keypoints, end=None):
    times = np.asarray(times, dtype=np.float64)
    points = np.asarray(keypoints, dtype=np.float32)
    if points.shape != (len(times), 17, 3) or not len(times):
        raise ValueError('Expected nonempty timestamps and [T,17,3] keypoints')
    if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError('Timestamps must be finite and strictly increasing')
    end = float(times[-1] if end is None else end)
    grid = end - np.arange(SLOTS-1, -1, -1) / HZ
    output = np.zeros((SLOTS, 17, 3), dtype=np.float32)
    used = set()
    for j, t in enumerate(grid):
        k = int(np.argmin(np.abs(times-t)))
        # A source observation may fill at most one slot: no repeated fake frames.
        if k not in used and abs(times[k]-t) <= 0.065:
            output[j] = points[k]
            used.add(k)
    bad = ~np.isfinite(output).all(axis=2)
    output[bad] = 0
    output[:, :, 2] = np.clip(output[:, :, 2], 0, 1)
    return output


def features(points):
    """Body-relative xy/conf + root trajectory; 54 channels, 16 time slots."""
    p = np.array(points, dtype=np.float32, copy=True)
    if p.shape != (SLOTS, 17, 3):
        raise ValueError('Expected [16,17,3]')
    good = (p[:, :, 2] >= 0.2) & np.isfinite(p).all(axis=2)
    p[~good] = 0
    roots = np.zeros((SLOTS, 2), dtype=np.float32)
    root_valid = good[:, 11] & good[:, 12]
    roots[root_valid] = p[root_valid, 11:13, :2].mean(axis=1)
    scales = []
    for i in range(SLOTS):
        if good[i].sum() >= 8:
            xy = p[i, good[i], :2]
            scales.append(np.max(xy[:, 1]) - np.min(xy[:, 1]))
    scale = max(float(np.median(scales)) if scales else 1., 1.)
    xy = np.clip((p[:, :, :2]-roots[:, None, :]) / scale, -3, 3)
    good &= root_valid[:, None]
    xy[~good] = 0
    p[:, :, :2] = xy
    p[:, :, 2] *= good
    valid_indices = np.flatnonzero(root_valid)
    origin = roots[valid_indices[0]] if len(valid_indices) else np.zeros(2)
    trajectory = np.clip((roots-origin)/scale, -3, 3)
    trajectory[~root_valid] = 0
    coverage = good.mean(axis=1, keepdims=True)
    return np.concatenate([p.reshape(SLOTS, 51), trajectory, coverage], axis=1).T.astype(np.float32, copy=True)


def box_jump(previous, current, dt):
    a, b = np.asarray(previous), np.asarray(current)
    ca, cb = (a[:2]+a[2:])/2, (b[:2]+b[2:])/2
    diag = max(float(np.linalg.norm(a[2:]-a[:2])), 1.)
    aa = max(float(np.prod(a[2:]-a[:2])), 1.)
    ab = max(float(np.prod(b[2:]-b[:2])), 1.)
    return dt < 0.3 and (np.linalg.norm(cb-ca)/diag > 0.8 or max(aa/ab, ab/aa) > 2.5)


class PersonWindows:
    def __init__(self, gap_seconds=0.6):
        self.tracks = {}
        self.gap_seconds = gap_seconds

    def clear(self):
        self.tracks.clear()

    def drop(self, track_id):
        self.tracks.pop(int(track_id), None)

    def push(self, track_id, timestamp, keypoints, bbox):
        track_id = int(track_id)
        if not math.isfinite(timestamp):
            raise ValueError('Timestamp must be finite')
        keypoints = np.asarray(keypoints, dtype=np.float32)
        if keypoints.shape != (17, 3):
            raise ValueError('Expected COCO17 xy confidence')
        # Drop long-absent IDs so memory stays bounded.
        for old in list(self.tracks):
            if timestamp-self.tracks[old][-1][0] > 5.0:
                self.drop(old)
        history = self.tracks.setdefault(track_id, deque())
        reason = None
        if history:
            dt = timestamp-history[-1][0]
            if dt <= 0:
                history.clear()
                return {'ready': False, 'reason': 'nonmonotonic_timestamp_reset'}
            if dt > self.gap_seconds:
                reason = 'tracking_gap_reset'
            elif box_jump(history[-1][2], bbox, dt):
                reason = 'identity_geometry_jump_reset'
            if reason:
                history.clear()
        history.append((float(timestamp), keypoints.copy(), np.asarray(bbox).copy()))
        while history and timestamp-history[0][0] > 2.15:
            history.popleft()
        span = history[-1][0]-history[0][0]
        p = resample([v[0] for v in history], [v[1] for v in history])
        valid = ((p[:, :, 2] >= 0.2).sum(axis=1) >= 8) & (p[:, 11, 2] >= .2) & (p[:, 12, 2] >= .2)
        coverage = float(valid.mean())
        ready = span >= 1.75 and coverage >= 0.6
        return {'ready': ready, 'reason': reason or (None if ready else 'insufficient_contiguous_history'),
                'window_start_s': timestamp-(SLOTS-1)/HZ, 'window_end_s': timestamp,
                'observation_span_s': span, 'slot_coverage': coverage,
                'unique_observations': len(history), 'features': features(p),
                'sample_rate_hz': HZ, 'time_slots': SLOTS}
