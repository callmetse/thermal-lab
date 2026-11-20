"""模型清单（P2-10）：权重 sha256、框架版本、有效参数、设备。

会话开始时由 worker 收集一次（进程内缓存），随 run 上下文交给数据面写进
`experiment_snapshots.model_manifest`；导出包用 `manifest_id` 引用它。
这样"独立机器能核对每次实验到底用了哪个模型、什么处理设置"，不再只有一句
`remote model hash unavailable`。

缺失的模型不抛异常：记 `status=unavailable` 与候选路径，让缺件本身成为可审计事实。
"""
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

FRAMEWORK_MODULES = ("ultralytics", "torch", "onnxruntime", "cv2", "numpy", "picamera2")
PARAM_KEYS = ("a_mode", "a_conf", "a_imgsz", "a_crop_imgsz", "a_crop_pose_imgsz", "a_crop_device",
              "a_crop_side", "pose_imgsz", "pose_max_det", "gate_imgsz", "rect",
              "min_person_confidence", "max_people", "min_person_height_ratio",
              "sample_fps", "capture_fps", "width", "height", "jpeg_quality", "encode_max_side",
              "llm_model", "collaboration")


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha_entry(path):
    """单文件返回 file 摘要；目录按 (相对路径, sha256) 排序后聚合，避免目录哈希不可比。"""
    path = Path(path)
    if path.is_file():
        return {"kind": "file", "sha256": sha_file(path), "bytes": path.stat().st_size, "files": 1}
    entries = []
    total = 0
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        entries.append(item.relative_to(path).as_posix() + ":" + sha_file(item))
        total += item.stat().st_size
    return {"kind": "tree", "sha256": hashlib.sha256("\n".join(entries).encode()).hexdigest(),
            "bytes": total, "files": len(entries)}


def frameworks():
    """返回 (版本, 状态)。状态区分 missing（没装）与 no_version（装了但没 __version__），
    否则 None 既可能是没装也可能是装了没版本号，核对时无法判定。"""
    versions, status = {}, {}
    for name in FRAMEWORK_MODULES:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", None)
            versions[name] = version
            status[name] = "ok" if version else "no_version"
        except Exception:  # noqa: BLE001 - 没装就是没装
            versions[name] = None
            status[name] = "missing"
    versions["python"] = sys.version.split()[0]
    status["python"] = "ok"
    return versions, status


def model_plan(config, role):
    suffix = "_ncnn_model" if role == "edge" else ".pt"
    override = os.environ.get("ABC_A_MODEL") or config.get("a_model")
    return [
        ("gate", "人员门控", [f"assets/yolo11n{suffix}"]),
        ("object_line", "A 线路目标检测", [p for p in (override, f"assets/yolo11n_object{suffix}") if p]),
        ("pose", "姿态关键点", [f"assets/yolo11n-pose{suffix}"]),
        ("clothing", "服装属性", [f"assets/clothing_best{suffix}"]),
        ("action", "时序动作（pytorch）", ["assets/action/best.pt"]),
    ]


def collect(config=None, root=None, role="edge", device=None, extras=None):
    config = config or {}
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    entries = []
    for model_id, label, candidates in model_plan(config, role):
        chosen = None
        for candidate in candidates:
            path = Path(candidate)
            path = path if path.is_absolute() else root / path
            if path.exists():
                chosen = path
                break
        if chosen is None:
            entries.append({"id": model_id, "label": label, "status": "unavailable",
                            "reason": "not_found", "candidates": list(candidates)})
            continue
        try:
            info = sha_entry(chosen)
        except OSError as error:
            entries.append({"id": model_id, "label": label, "status": "unavailable",
                            "reason": "unreadable: " + type(error).__name__, "candidates": list(candidates)})
            continue
        entries.append({"id": model_id, "label": label, "status": "ok",
                        "path": str(chosen.relative_to(root)) if str(chosen).startswith(str(root)) else str(chosen),
                        **info})
    provenance = None
    provenance_file = root / "PROVENANCE.json"
    if provenance_file.is_file():
        provenance = {"path": "PROVENANCE.json", "sha256": sha_file(provenance_file)}
    versions, status = frameworks()
    return {
        "schema_version": "1.0",
        "role": role,
        "device": device or ("pi-cpu" if role == "edge" else "cuda:0"),
        "generated_at_ms": int(time.time() * 1000),
        "host": platform.node(),
        "machine": platform.machine(),
        "models": entries,
        "provenance_file": provenance,
        "frameworks": versions,
        "framework_status": status,
        "params": {key: config[key] for key in PARAM_KEYS if key in config},
        "assets_provenance_sha256": (provenance or {}).get("sha256"),
        "extras": extras or None,
    }


def manifest_id(manifest):
    """清单 ID = 规范序列化后的 sha256；worker/数据面/导出包三处必须用同一算法。"""
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
