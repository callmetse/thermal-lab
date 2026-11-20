"""P1-06 回归：数据面（批次库/归档/上传/环境曲线）与视觉进程解耦。

复现审查原文 P1-06（修复前必然失败）：
- 没有独立的 `lab.dataset_api`：批次库、归档、上传、数据页都长在 vision-edge 进程里；
- `LocalPreview` 一启动就建批次库与 `collection_routes`，`supervisorctl stop vision-edge`
  等于把 `/data`、批次列表、导出、收藏/回收站、环境曲线一起停掉。

验收口径（本文件即断言）：
1. 数据面能独立起来、独立完成"开始/结束会话"；
2. 停掉预览（模拟停 vision-edge）后，批次列表/详情/导出/上传仍然可用；
3. 预览进程不再内嵌数据路由（收到请求应 404，而不是自己处理）；
4. 部署结构（Caddy 28992、supervisor 的 dataset-api program）真的改对了。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab import video_uploads  # noqa: E402
from lab.local_preview import LocalPreview  # noqa: E402

TEST_CONFIG = {"project": "abc20261102", "topic_root": "labs/abc20261102/v1",
               "mqtt_host": "127.0.0.1", "mqtt_port": 18830, "llm_model": "qwen2.5:7b",
               "sample_fps": 5, "capture_fps": 15, "max_people": 0, "pose_max_det": 4,
               "min_person_confidence": 0.2, "min_person_height_ratio": 0.1,
               "replay_file": "data/replay.mp4", "width": 1280, "height": 720}


def sandbox():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data" / "uploads").mkdir(parents=True)
    source = tmp / "env_sensor.sqlite3"
    with sqlite3.connect(str(source)) as db:
        db.executescript('''CREATE TABLE measurements(id INTEGER PRIMARY KEY, zone_id TEXT, sensor_id TEXT,
                            event_id TEXT, received_at_ms INTEGER, quality TEXT, values_json TEXT);''')
        db.commit()
    (tmp / "config.json").write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
    return tmp, source


def http_json(url, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return response.status, json.loads(body or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body or b"{}")
        except ValueError:
            return error.code, {"raw": body.decode("utf-8", "replace")}


def test_dataset_api_owns_session_lifecycle():
    """数据面自己就能开始/结束会话：这是脱离视觉进程的前提。"""
    tmp, source = sandbox()
    old_config = os.environ.get("ABC_CONFIG")
    os.environ["ABC_CONFIG"] = str(tmp / "config.json")
    from lab.dataset_api import DatasetApi

    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    try:
        base = "http://127.0.0.1:%d" % api.port
        status, state = http_json(base + "/api/dataset/session", {"run_id": "r1", "context": {"run_id": "r1"}})
        assert status == 200, (status, state)
        assert state["active_batch_id"] == "r1", state
        status, listing = http_json(base + "/api/collection-batches")
        assert status == 200 and listing["batches"][0]["batch_id"] == "r1", listing
        assert listing["batches"][0]["status"] == "active", listing
        status, state = http_json(base + "/api/dataset/session", {"run_id": None})
        assert status == 200 and state["active_batch_id"] is None, state
        status, detail = http_json(base + "/api/collection-batches/r1")
        assert status == 200 and detail["status"] == "completed", detail
        print("PASS 数据面独立管理会话：start→active→finish→completed")
    finally:
        api.stop()
        if old_config is None:
            os.environ.pop("ABC_CONFIG", None)
        else:
            os.environ["ABC_CONFIG"] = old_config


def test_data_survives_preview_shutdown():
    """停掉预览（等价 supervisorctl stop vision-edge）后，数据面仍可查询/导出/上传。"""
    tmp, source = sandbox()
    old_config = os.environ.get("ABC_CONFIG")
    old_root, old_inspect = video_uploads.ROOT, video_uploads.inspect_video
    os.environ["ABC_CONFIG"] = str(tmp / "config.json")
    video_uploads.ROOT = tmp
    video_uploads.inspect_video = lambda path: {"duration_s": 1.0, "fps": 25.0, "width": 64, "height": 64}
    os.environ["ABC_UPLOAD_QUOTA_BYTES"] = "104857600"
    from lab.dataset_api import DatasetApi

    api = DatasetApi(root=tmp, env_db=source, port=0).start()
    preview = LocalPreview(port=0, dataset_api_url="http://127.0.0.1:%d" % api.port).start()
    try:
        base = "http://127.0.0.1:%d" % api.port
        preview.set_run("r2", {"run_id": "r2"})
        from lab.archive_ingest import ArchiveIngest
        assert preview.batch_store is None, "预览进程不应再持有批次库"
        assert isinstance(preview.archive, ArchiveIngest), (
            "拆分模式下预览必须挂归档投递入口（R2），实际 %r" % (preview.archive,))
        # 预览自己不该有数据路由
        status, _ = http_json("http://127.0.0.1:%d/api/collection-batches" % preview.port)
        assert status == 404, "预览进程内不应再内嵌 /api/collection-batches，实际 %s" % status
        preview.close()          # ← 相当于 stop vision-edge
        status, listing = http_json(base + "/api/collection-batches")
        assert status == 200 and listing["batches"][0]["batch_id"] == "r2", listing
        status, body = http_json(base + "/api/collection-batches/r2/summary")
        assert status == 200 and body["snapshot_version"] == "atomic-v1", body
        # 上传（数据面自己做）
        boundary = "----p106"
        payload = (("--%s\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n1\r\n--%s--\r\n") % (boundary, boundary)).encode()
        request = urllib.request.Request(base + "/api/video-uploads", data=b"y" * 700_000,
                                         headers={"Content-Type": "video/mp4"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            upload = json.loads(response.read())
        assert response.status == 201 and upload["bytes"] == 700_000, upload
        print("PASS 停预览后数据面仍可用：列表/摘要 200、上传 201（预览内数据路由 404）")
    finally:
        api.stop()
        preview.close()
        video_uploads.ROOT, video_uploads.inspect_video = old_root, old_inspect
        os.environ.pop("ABC_UPLOAD_QUOTA_BYTES", None)
        if old_config is None:
            os.environ.pop("ABC_CONFIG", None)
        else:
            os.environ["ABC_CONFIG"] = old_config


def test_preview_keeps_only_preview_routes():
    """内嵌模式（默认）仍要能用：不配 dataset_api 时行为不能回退。"""
    tmp, source = sandbox()
    old_config = os.environ.get("ABC_CONFIG")
    os.environ["ABC_CONFIG"] = str(tmp / "config.json")
    os.environ["ABC_ENV_DB"] = str(source)
    try:
        preview = LocalPreview(port=0).start()
        try:
            assert preview.batch_store is not None, "内嵌模式下仍应自建批次库（兼容旧部署）"
            status, listing = http_json("http://127.0.0.1:%d/api/collection-batches" % preview.port)
            # 内嵌模式只验"路由还在、返回结构对"，不假定库是空的（Pi 上有真实批次）
            assert status == 200 and isinstance(listing.get("batches"), list), listing
            print("PASS 内嵌模式保留：未配置 dataset_api 时批次接口照旧可用")
        finally:
            preview.close()
    finally:
        os.environ.pop("ABC_ENV_DB", None)
        if old_config is None:
            os.environ.pop("ABC_CONFIG", None)
        else:
            os.environ["ABC_CONFIG"] = old_config


def test_deployment_structure_routes_to_dataset_api():
    """部署结构：Caddy 有 28992/28990 两端，supervisor 有 dataset-api program。
    具体路径分流行为由 tests/test_control_plane_routes.py 断言（R1），这里不重复文本匹配。"""
    caddy = (ROOT / "deploy" / "console.Caddyfile").read_text(encoding="utf-8")
    assert "127.0.0.1:28992" in caddy, "Caddyfile 没有指向数据面 28992"
    assert "127.0.0.1:28990" in caddy, "Caddyfile 丢了预览 28990"
    supervisor = (ROOT / "deploy" / "supervisord.ini").read_text(encoding="utf-8")
    assert "[program:dataset-api]" in supervisor, "supervisord.ini 缺少 dataset-api program"
    assert "-m lab.dataset_api" in supervisor, "dataset-api 的启动命令不对"
    assert "[program:vision-edge]" in supervisor, "vision-edge program 不应消失"
    print("PASS 部署结构：Caddy 28992/28990、supervisor dataset-api 均已就位")


if __name__ == "__main__":
    test_dataset_api_owns_session_lifecycle()
    test_data_survives_preview_shutdown()
    test_preview_keeps_only_preview_routes()
    test_deployment_structure_routes_to_dataset_api()
