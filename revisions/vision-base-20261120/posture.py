"""Geometry helper copied for independent evaluation; no MET mapping."""
import math

def estimate_posture(p, bbox_h=None, kp_thr=0.30):
    """从 COCO17 关键点估计静态姿态：lying / seated / standing / unknown。"""
    kps = p.get("keypoints_coco17")
    if not kps or len(kps) < 17:
        return {"state": "unknown", "reason": "no_keypoints"}
    P = {i: (float(x), float(y), float(c)) for i, (x, y, c) in enumerate(kps)}
    need = (5, 6, 11, 12, 13, 14, 15, 16)
    vis = sum(1 for i in need if P[i][2] >= kp_thr) / len(need)

    def mid(a, b):
        return ((P[a][0] + P[b][0]) / 2.0, (P[a][1] + P[b][1]) / 2.0)

    def angle(a, b, c):
        v1 = (P[a][0] - P[b][0], P[a][1] - P[b][1])
        v2 = (P[c][0] - P[b][0], P[c][1] - P[b][1])
        n1 = math.hypot(*v1); n2 = math.hypot(*v2)
        if n1 < 1e-6 or n2 < 1e-6:
            return None
        cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        return math.degrees(math.acos(cosv))

    ms, mh = mid(5, 6), mid(11, 12)
    torso_dx = abs(ms[0] - mh[0]); torso_dy = abs(ms[1] - mh[1])
    torso_from_vertical = math.degrees(math.atan2(torso_dx, max(torso_dy, 1e-6)))

    knees = [k for k in (angle(11, 13, 15), angle(12, 14, 16)) if k is not None]
    knee = min(knees) if knees else None
    ankles_y = [P[i][1] for i in (15, 16) if P[i][2] >= kp_thr]
    hip_ratio = (((sum(ankles_y) / len(ankles_y)) - mh[1]) / bbox_h
                 if (ankles_y and bbox_h) else None)

    state, reason = "unknown", None
    if vis < 0.5:
        reason = "keypoints_low_visibility"
    elif torso_from_vertical >= 55:
        state = "lying"
    elif knee is not None and knee < 120 and (hip_ratio is None or hip_ratio < 0.36):
        state = "seated"
    elif knee is not None and knee >= 145 and (hip_ratio is None or hip_ratio >= 0.30):
        state = "standing"
    else:
        reason = "geometry_ambiguous"
    return {"state": state, "reason": reason, "keypoint_visibility": round(vis, 2),
            "knee_angle_deg": (round(knee, 1) if knee is not None else None),
            "torso_deg_from_vertical": round(torso_from_vertical, 1),
            "hip_ankle_ratio": (round(hip_ratio, 3) if hip_ratio is not None else None)}
