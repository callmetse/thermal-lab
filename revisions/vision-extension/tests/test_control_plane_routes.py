"""R1 回归：Caddy 必须按职责显式分流，控制接口不能落到数据面。

复现复核意见 R1（修复前必红）：
旧配置用 `@dataset path /data /api/*` 把除预览外的所有 /api/* 都转到 28992，
而 DatasetApi 没有 /api/state、/api/raw-state、/api/start、/api/stop、/api/ack 的实现，
现场登录后这些控制接口直接 404。

本测试不匹配配置文本，而是把 Caddyfile 解析成"路径 → 上游端口"的路由表，
再对代表性 URL 断言职责分流。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CADDY = ROOT / "deploy" / "console.Caddyfile"

PREVIEW_PORT = 28990
DATASET_PORT = 28992
CONSOLE_PORT = 28881


def parse_routes(text):
    """返回 [(tokens, port)], default_port。只认本工程用到的写法。"""
    matchers, default_port = [], None
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"@(\w+)\s+path\s+(.+)$", line)
        if match:
            tokens = match.group(2).strip().split()
            pending = [token for token in tokens if token not in ("{", "}")]
            continue
        match = re.match(r"reverse_proxy\s+127\.0\.0\.1:(\d+)", line)
        if match:
            port = int(match.group(1))
            if pending is not None:
                matchers.append((pending, port))
                pending = None
            else:
                default_port = port
    return matchers, default_port


def match_port(path, matchers, default_port):
    for tokens, port in matchers:
        for token in tokens:
            if token.endswith("*"):
                if path.startswith(token[:-1]):
                    return port
            elif path == token:
                return port
    return default_port


def routes():
    matchers, default_port = parse_routes(CADDY.read_text(encoding="utf-8"))
    assert default_port == CONSOLE_PORT, "默认分支应回落到控制台 %s，实际 %s" % (CONSOLE_PORT, default_port)
    return matchers, default_port


def test_control_endpoints_reach_console():
    matchers, default_port = routes()
    for path in ("/lab", "/api/state", "/api/raw-state", "/api/start", "/api/stop",
                 "/api/ack", "/api/session", "/api/settings"):
        port = match_port(path, matchers, default_port)
        assert port == CONSOLE_PORT, "%s 应到控制台 %s，实际 %s" % (path, CONSOLE_PORT, port)
    print("PASS R1 控制接口归控制台：/api/state、/api/raw-state、start/stop/ack 全部 → %s" % CONSOLE_PORT)


def test_data_endpoints_reach_dataset_api():
    matchers, default_port = routes()
    expected = {
        "/data": DATASET_PORT,
        "/api/video-uploads": DATASET_PORT,
        "/api/collection-batches": DATASET_PORT,
        "/api/collection-batches/abc/export": DATASET_PORT,
        "/api/collection-batches/abc/environment": DATASET_PORT,
        "/api/environment/latest": DATASET_PORT,
        "/api/environment/history": DATASET_PORT,
        "/api/environment/longterm/history": DATASET_PORT,
    }
    for path, want in expected.items():
        got = match_port(path, matchers, default_port)
        assert got == want, "%s 应到数据面 %s，实际 %s" % (path, want, got)
    print("PASS R1 数据接口归数据面：/data、上传、批次、环境曲线 → %s" % DATASET_PORT)


def test_preview_endpoints_stay_on_preview():
    matchers, default_port = routes()
    for path in ("/live-video", "/api/preview", "/api/preview-frame", "/api/preview-meta"):
        got = match_port(path, matchers, default_port)
        assert got == PREVIEW_PORT, "%s 应到预览 %s，实际 %s" % (path, PREVIEW_PORT, got)
    print("PASS R1 预览接口留在预览进程：/live-video 与 /api/preview* → %s" % PREVIEW_PORT)


def test_no_catch_all_api_wildcard():
    matchers, _ = routes()
    wildcard = [tokens for tokens, _ in matchers if "/api/*" in tokens]
    assert not wildcard, "不允许再用 /api/* 通配分流：%s" % wildcard
    print("PASS R1 没有 /api/* 通配：分流按职责白名单")


def test_internal_dataset_endpoint_not_exposed():
    matchers, default_port = routes()
    for path in ("/api/dataset/state", "/api/dataset/session", "/api/dataset/records"):
        port = match_port(path, matchers, default_port)
        assert port != DATASET_PORT, (
            "%s 是数据面内部接口，不应作为前端入口暴露（实际被分流到 %s）" % (path, port))
    print("PASS R1 内部接口不外露：/api/dataset/* 不被转发到数据面")


if __name__ == "__main__":
    test_control_endpoints_reach_console()
    test_data_endpoints_reach_dataset_api()
    test_preview_endpoints_stay_on_preview()
    test_no_catch_all_api_wildcard()
    test_internal_dataset_endpoint_not_exposed()
