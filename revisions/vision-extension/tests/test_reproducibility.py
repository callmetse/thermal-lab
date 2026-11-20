"""P2-10 回归：复现包（模型清单 + 覆盖声明）与 Schema 强校验。

复现审查原文 P2-10（修复前必然失败）：
1. 快照只有一句 `remote model hash unavailable`，独立机器核对不了"用的哪个模型"；
2. `record_schema.json` 只校验外壳，`payload` 任意 object；运行时没有任何校验，
   倒序 bbox、超范围置信度会被原样收下；
3. 导出包没有 manifest，也没有"没保存什么"的 coverage 声明。

用法：python tests/test_reproducibility.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab import record_schema  # noqa: E402
from lab.collection_batches import CollectionBatches  # noqa: E402
from lab.experiment_archive import ExperimentArchive  # noqa: E402
from lab.model_manifest import collect as collect_manifest, manifest_id  # noqa: E402

T0 = 1_800_000_000_000
TEST_CONFIG = {"project": "abc20261102", "topic_root": "labs/abc20261102/v1",
               "mqtt_host": "127.0.0.1", "mqtt_port": 18830, "llm_model": "qwen2.5:7b",
               "sample_fps": 5, "capture_fps": 15, "max_people": 0, "pose_max_det": 4,
               "min_person_confidence": 0.2, "min_person_height_ratio": 0.1,
               "replay_file": "data/replay.mp4", "width": 1280, "height": 720,
               "a_mode": "detect", "a_conf": 0.25, "pose_imgsz": 640}

MANIFEST = {
    "schema_version": "1.0", "role": "edge", "device": "pi-cpu", "generated_at_ms": 1794917000000,
    "host": "raspberrypi", "machine": "aarch64",
    "models": [{"id": "gate", "label": "人员门控", "status": "ok", "kind": "tree",
                "sha256": "a" * 64, "bytes": 11_000_000, "files": 3, "path": "assets/yolo11n_ncnn_model"}],
    "provenance_file": {"path": "PROVENANCE.json", "sha256": "b" * 64},
    "frameworks": {"ultralytics": "8.3.0", "torch": "2.5.0", "python": "3.13.5"},
    "params": {"a_mode": "detect", "pose_imgsz": 640},
}


class Sandbox:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data" / "uploads").mkdir(parents=True)
        source = self.tmp / "env.sqlite3"
        with sqlite3.connect(str(source)) as db:
            db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                                event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
            db.commit()
        (self.tmp / "config.json").write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
        # 导出 manifest 会登记"当时跑的代码"，沙箱里造同名文件才能验证这一段
        (self.tmp / "lab").mkdir(exist_ok=True)
        for name in ("worker.py", "experiment_archive.py", "dataset_api.py", "local_preview.py",
                     "video_uploads.py", "record_schema.py", "model_manifest.py", "collection_batches.py"):
            (self.tmp / "lab" / name).write_text("# " + name, encoding="utf-8")
        self.source = source
        self.store = CollectionBatches(self.tmp / "batches.sqlite3", source)
        self.archive = ExperimentArchive(self.store, self.tmp)
        with sqlite3.connect(str(self.store.path)) as db:
            db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                          source_cursor,last_sync_ms,run_json) VALUES(?,?,?,?,?,?,'completed',0,?,?)''',
                       ("b1", "r1", "工位", "复现包测试", "lab", T0, T0, json.dumps({"arm": "B"})))
            db.commit()
        self._old_config = os.environ.get("ABC_CONFIG")
        os.environ["ABC_CONFIG"] = str(self.tmp / "config.json")

    def close(self):
        self.archive.close()
        if self._old_config is None:
            os.environ.pop("ABC_CONFIG", None)
        else:
            os.environ["ABC_CONFIG"] = self._old_config


def observation(seq=1, **overrides):
    body = {"collection_batch_id": "b1", "producer_instance_id": "p", "observation_seq": seq,
            "source_frame_id": seq, "tracks": [{"bbox": [10.0, 20.0, 30.0, 40.0], "confidence": 0.5,
                                                "class_name": "cup", "class_id": 41, "track_id": 1}]}
    body.update(overrides)
    return body


def read_zip(path):
    with zipfile.ZipFile(str(path)) as archive:
        return set(archive.namelist()), {name: archive.read(name) for name in archive.namelist()}


# ---------------------------------------------------------------- 1. 模型清单进快照

def test_model_manifest_lands_in_snapshot():
    box = Sandbox()
    try:
        box.archive.begin({"run_id": "r1", "collection_batch_id": "b1", "source": "camera",
                           "model_manifest": MANIFEST})
        with sqlite3.connect(str(box.store.path)) as db:
            snapshot = json.loads(db.execute("SELECT json FROM experiment_snapshots WHERE batch_id='b1'").fetchone()[0])
        assert snapshot["model_manifest_id"] == manifest_id(MANIFEST), snapshot.get("model_manifest_id")
        assert snapshot["model_manifest"]["models"][0]["sha256"] == "a" * 64, snapshot["model_manifest"]
        assert "remote model hash unavailable" not in snapshot["model_provenance"], snapshot["model_provenance"]
        summary = box.archive.summary("b1")
        assert summary["model_manifest_id"] == manifest_id(MANIFEST), summary["model_manifest_id"]
        assert summary["coverage"]["raw_camera_video"] is False, summary["coverage"]
        print("PASS 模型清单进快照：manifest_id 与摘要一致，coverage 已声明")

        # 旧客户端不带清单：如实写 unavailable，不伪造
        box.archive.begin({"run_id": "legacy", "collection_batch_id": "legacy", "source": "camera"})
        with sqlite3.connect(str(box.store.path)) as db:
            legacy = json.loads(db.execute("SELECT json FROM experiment_snapshots WHERE batch_id='legacy'").fetchone()[0])
        assert legacy["model_manifest_id"] is None and legacy["model_manifest"] is None, legacy
        assert "unavailable" in legacy["model_provenance"], legacy["model_provenance"]
        print("PASS 无清单时如实标 unavailable（没有伪造 manifest_id）")
    finally:
        box.close()


# ---------------------------------------------------------------- 2. 非法记录隔离

def test_invalid_records_are_quarantined():
    box = Sandbox()
    try:
        box.archive.begin({"run_id": "r1", "collection_batch_id": "b1", "source": "camera"})
        box.archive.submit("observation", observation(1))
        box.archive.submit("observation", observation(2, tracks=[{"bbox": [10.0, 10.0, 5.0, 20.0], "confidence": 0.5}]))
        box.archive.submit("observation", observation(3, tracks=[{"bbox": [1.0, 2.0, 3.0, 4.0], "confidence": 1.4}]))
        box.archive.submit("observation", {"collection_batch_id": "b1", "producer_instance_id": "p", "observation_seq": 4})
        box.archive.pending.join()
        summary = box.archive.summary("b1")
        with sqlite3.connect(str(box.store.path)) as db:
            rows = db.execute("SELECT COUNT(*) FROM experiment_records WHERE batch_id='b1'").fetchone()[0]
            error = db.execute("SELECT error FROM experiment_errors WHERE batch_id='b1'").fetchone()
        assert rows == 1, "只有合法记录能进正式表，实际 %d 条" % rows
        assert summary["observation_count"] == 1, summary["observation_count"]
        assert summary["rejected_records"] == 3, summary["rejected_records"]
        assert error and "schema_invalid" in error[0], error
        print("PASS 非法记录隔离：有效 1 条入库，3 条进 experiment_errors（%s）" % (error[0][:60]))
    finally:
        box.close()


def test_schema_engines_agree():
    bad_order = {"producer_instance_id": "p", "observation_seq": 1,
                 "tracks": [{"bbox": [10.0, 10.0, 5.0, 20.0], "confidence": 0.5}]}
    bad_type = {"producer_instance_id": "p", "observation_seq": 1,
                "tracks": [{"bbox": [1.0, 2.0, 3.0, 4.0], "confidence": "high"}]}
    good = observation(1)
    saved_flag, saved_module = record_schema.HAS_JSONSCHEMA, getattr(record_schema, "jsonschema")
    results = {}
    try:
        for engine in ("jsonschema", "builtin"):
            record_schema.HAS_JSONSCHEMA = engine == "jsonschema"
            record_schema.jsonschema = saved_module if engine == "jsonschema" else None
            rejected = []
            for name, body in (("order", bad_order), ("type", bad_type)):
                try:
                    record_schema.validate("observation", body)
                    rejected.append(name + ":accepted")
                except record_schema.RecordInvalid:
                    rejected.append(name + ":rejected")
            record_schema.validate("observation", good)   # 合法记录必须通过
            results[engine] = rejected
    finally:
        record_schema.HAS_JSONSCHEMA, record_schema.jsonschema = saved_flag, saved_module
    assert results["jsonschema"] == results["builtin"] == ["order:rejected", "type:rejected"], results
    print("PASS 两套校验引擎一致：倒序 bbox 与错误类型都被拒，合法记录通过（%s）"
          % ("jsonschema" if saved_flag else "builtin"))


# ---------------------------------------------------------------- 3. 导出包 manifest/coverage

def test_export_manifest_and_coverage():
    box = Sandbox()
    try:
        box.archive.begin({"run_id": "r1", "collection_batch_id": "b1", "source": "camera",
                           "model_manifest": MANIFEST})
        box.archive.submit("observation", observation(1))
        box.archive.pending.join()
        names, data = read_zip(box.archive.export("b1"))
        assert "manifest.json" in names, sorted(names)
        manifest = json.loads(data["manifest.json"])
        summary = json.loads(data["summary.json"])
        assert manifest["manifest_id"] == summary["model_manifest_id"] == manifest_id(MANIFEST), manifest["manifest_id"]
        assert manifest["model"]["models"][0]["status"] == "ok", manifest["model"]
        assert manifest["coverage"]["raw_camera_video"] is False, manifest["coverage"]
        assert manifest["coverage"]["cloud_events"], manifest["coverage"]
        assert manifest["software"] and "lab/experiment_archive.py" in manifest["software"], manifest["software"]
        assert manifest["config"]["pose_imgsz"] == 640, manifest["config"]
        schema = json.loads(data["record_schema.json"])
        assert "observation" in schema["$defs"], sorted(schema)
        assert "bbox" in schema["$defs"]["observation"]["properties"]["tracks"]["items"]["properties"], schema["$defs"]
        checks = json.loads(data["checksums.json"])
        assert "manifest.json" in checks and set(checks) <= names, sorted(checks)
        print("PASS 导出包：manifest/coverage/schema 齐全，manifest_id 与库内快照一致")
    finally:
        box.close()


# ---------------------------------------------------------------- 4. 清单收集器

def test_model_manifest_collector():
    tmp = Path(tempfile.mkdtemp())
    gate = tmp / "assets" / "yolo11n_ncnn_model"
    gate.mkdir(parents=True)
    (gate / "model.ncnn.param").write_text("param", encoding="utf-8")
    (gate / "model.ncnn.bin").write_bytes(b"weights" * 100)
    (tmp / "assets" / "action").mkdir(parents=True)
    (tmp / "assets" / "action" / "best.pt").write_bytes(b"checkpoint")
    manifest = collect_manifest(dict(TEST_CONFIG), tmp, role="edge")
    models = {entry["id"]: entry for entry in manifest["models"]}
    assert models["gate"]["status"] == "ok" and models["gate"]["kind"] == "tree", models["gate"]
    assert models["gate"]["files"] == 2, models["gate"]
    assert models["action"]["status"] == "ok" and models["action"]["kind"] == "file", models["action"]
    assert models["object_line"]["status"] == "unavailable", models["object_line"]
    assert models["object_line"]["candidates"], models["object_line"]
    assert manifest["device"] == "pi-cpu" and manifest["role"] == "edge", manifest
    assert manifest["frameworks"]["python"] and "cv2" in manifest["frameworks"], manifest["frameworks"]
    assert manifest["framework_status"]["python"] == "ok", manifest["framework_status"]
    assert set(manifest["framework_status"].values()) <= {"ok", "missing", "no_version"}, manifest["framework_status"]
    assert manifest["params"]["a_mode"] == "detect" and "replay_file" not in manifest["params"], manifest["params"]
    first = manifest_id(manifest)
    (gate / "model.ncnn.bin").write_bytes(b"changed")
    assert manifest_id(collect_manifest(dict(TEST_CONFIG), tmp, role="edge")) != first, "权重变了 manifest_id 必须变"
    print("PASS 清单收集器：目录/文件哈希、缺件标注、参数与设备齐全，权重变化会改 manifest_id")


if __name__ == "__main__":
    test_model_manifest_lands_in_snapshot()
    test_invalid_records_are_quarantined()
    test_schema_engines_agree()
    test_export_manifest_and_coverage()
    test_model_manifest_collector()
