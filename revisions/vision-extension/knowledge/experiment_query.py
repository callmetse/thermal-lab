"""Read-only experiment Q&A for the pilot. Run on a trusted analysis host."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.knowledge_retrieval import build, retrieve

RESTART_RUN = "0737ac79-5374-49b6-93aa-29e50fdd98c3"
EVIDENCE = "05_实测结果/decoupling_acceptance_20261118"
BATCH_PREFIX = "/api/collection-batches/"
ENVIRONMENT_LATEST = "/api/environment/latest?zone_id=lab"
RESTART_IDS = ("d-restart-adoption", "d-restart-export")
REVIEWED_EVIDENCE_SHA256 = {
    "dataset_restart_d.json": "ee34fd99f36ee99ccc0ee9485ee7150ae0b2aa613cd78f241f8f5425068dce2c",
    "channel_D_restart.json": "cf182a962470a56e090b551b39caea7638b406e3a8956a2ffe73e351806e9eb0",
}

# Match a complete supported request before choosing a numeric field.
OBSERVATION = r"(?:观测(?:记录|结果)?|观察记录)"
QUANTITY = r"(?:多少(?:条)?|几条)"
ARCHIVE_COUNT = re.compile(
    rf"""
    (?:请问|请告诉我)?
    (?:(?P<channel>[A-D])(?:通道|批次|路)(?:的)?)?
    (?:(?P<restart>(?:数据服务)?重启(?:之后|后))[,]?)?
    (?:(?:这个|这次|这|当前|所选|选中|原)(?:实验)?批次(?:的)?)?
    (?P<final>最终|累计|总共|一共|合计|总计)?
    (?:
        (?:已)?(?:归档|记录|收到并归档)(?:了)?{QUANTITY}
        (?P<producer_after>云端(?:产生的)?)?{OBSERVATION}
      | (?P<producer_before>云端(?:产生的)?)?{OBSERVATION}
        (?:已)?(?:归档|记录)(?:了)?{QUANTITY}
      | (?:已)?归档(?:的)?(?P<producer_noun>云端(?:产生的)?)?{OBSERVATION}
        (?:的)?(?:总数|数量|条数)(?:是)?多少
      | (?:共有|有){QUANTITY}(?:已归档的)?{OBSERVATION}
      | (?:已归档的)?{OBSERVATION}(?:有{QUANTITY}|(?:的)?(?:总数|数量|条数)(?:是)?多少)
    ) (?:呢)?
    (?P<continuation>[?,。](?:怎样|如何)证明(?:批次继续|仍是同一批(?:次)?|还是同一批(?:次)?))?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read_json(base, path, authorization=None):
    origin = urlsplit(base)
    if (origin.scheme not in ("http", "https") or not origin.hostname or origin.username
            or origin.password or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise ValueError("Expected a dataset origin with no credentials or path")
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, headers=headers), timeout=5) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Batch response too large")
    return json.loads(data)


def read_summary(base, batch_id, authorization=None):
    """Fetch one batch through a fixed GET route. The caller configures the origin."""
    return _read_json(base, BATCH_PREFIX + str(UUID(batch_id)) + "/summary", authorization)


def restart_documents(repo):
    """Index the reviewed acceptance pair. Keep structured facts and source hashes."""
    docs = []
    for key, title, section, name in (
        (RESTART_IDS[0], "D通道重启后沿用原实验批次", "health_after_start", "dataset_restart_d.json"),
        (RESTART_IDS[1], "D通道重启验收后的云端观测归档数量", "export_producers", "channel_D_restart.json"),
    ):
        path = f"{EVIDENCE}/{name}"
        raw = (repo / path).read_bytes().replace(b"\r\n", b"\n")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != REVIEWED_EVIDENCE_SHA256[name]:
            raise ValueError(f"Reviewed evidence changed: {path}")
        record = json.loads(raw)
        if record["run_id"] != RESTART_RUN:
            raise ValueError(f"Wrong run in {path}")
        if key == RESTART_IDS[0]:
            health = record["health_after_start"]
            if health["active_run_id"] != RESTART_RUN or health["active_batch_id"] != RESTART_RUN:
                raise ValueError("Restart did not retain the run and batch ID")
            if health["adopted_on_restart"] is not True:
                raise ValueError("Restart adoption evidence missing")
            facts = {"before_stop": record["recorded_before_stop"], "adopted_on_restart": True}
            content = "数据服务重启后的健康检查确认原运行编号和批次编号，并报告成功接管。"
        else:
            if record["export_http"] != 200 or record["channel"] != "D":
                raise ValueError("D export verification missing")
            facts = {"cloud_observations": record["export_producers"]["cloud"],
                     "total_observations": record["export_observations"]}
            if record["archive_observations"] != facts["total_observations"]:
                raise ValueError("Archive/export disagreement")
            content = "D通道数据服务重启验收的完成批次，导出记录按生产方统计已归档的云端观测。"
        if any(type(value) is not int or value < 0
               for name, value in facts.items() if name != "adopted_on_restart"):
            raise ValueError("Invalid acceptance counts")
        docs.append({"id": key, "title": title, "section": section, "source": path,
                     "text": content, "reviewed": True, "effective": True,
                     "revision": "c3e4f8d", "effective_from": "2026-11-18",
                     "source_sha256": digest,
                     "batch_id": RESTART_RUN, "facts": facts})
    return docs


def insufficient(reason, answer="现有证据不足，无法确认这项结论。"):
    return {"status": "insufficient_evidence", "reason": reason,
            "answer": answer, "citations": []}


def current_environment(question, read):
    if read is None:
        return insufficient("sensor_api_required")
    try:
        payload = read()
    except (URLError, TimeoutError, ValueError):
        return insufficient("sensor_api_unavailable")
    if not isinstance(payload, dict) or payload.get("zone_id") != "lab":
        return insufficient("invalid_sensor_response")
    fields, thresholds, now = payload.get("fields"), payload.get("stale_after_ms"), payload.get("now_ms")
    if not isinstance(fields, dict) or not isinstance(thresholds, dict) or type(now) is not int:
        return insufficient("invalid_sensor_response")
    values = {}
    messages = []
    for field, pattern, label, sensor, unit in (
        ("temperature_c", r"温度|温湿度", "温度", "sht4x", "°C"),
        ("humidity_rh", r"湿度|温湿度", "湿度", "sht4x", "%RH"),
        ("co2_ppm", r"二氧化碳|co₂|co2", "二氧化碳", "scd41", "ppm"),
    ):
        if not re.search(pattern, question, re.I):
            continue
        record = fields.get(field) or {}
        age, value, limit, timestamp = (record.get("age_ms"), record.get("value"),
                                        thresholds.get(field), record.get("data_time_ms"))
        if (record.get("present") is True and record.get("stale") is False and record.get("quality") == "ok"
                and record.get("sensor_id") == sensor and type(age) is int and type(limit) is int
                and 0 <= age <= limit and type(value) in (int, float) and math.isfinite(value)
                and type(timestamp) is int):
            values[field] = {"value": value, "unit": unit, "sensor_id": sensor,
                             "data_time_ms": timestamp, "age_ms": age}
            messages.append(f"{label} {value:g} {unit}")
        else:
            values[field] = None
            messages.append(f"{label}未知，当前缺少新鲜读数")
    if not values or all(value is None for value in values.values()):
        return insufficient("no_fresh_sensor_readings")
    return {"status": "answered", "answer": "实验室当前读数为" + "，".join(messages) + "。",
            "facts": {"zone_id": "lab", "gateway_now_ms": now, "values": values},
            "citations": [{"id": "environment-latest", "title": "实验室传感器读数",
                           "source": ENVIRONMENT_LATEST, "section": "fields"}]}


def query(index, question, batch_id=None, get_summary=None, get_environment=None):
    if not question.strip() or len(question) > 500:
        raise ValueError("Enter a question of 1–500 characters")
    if not batch_id:
        current = re.search(r"当前|现在|此刻|实时", question) and re.search(r"温度|湿度|温湿度|二氧化碳|co₂|co2", question, re.I)
        if current and not re.search(r"历史|比较|趋势|平均|最高|最低|为什么|原因|原理|校准", question):
            return current_environment(question, get_environment)
        citations = retrieve(index, question, min_score=0.12)
        return ({"status": "sources", "answer": "找到以下项目资料，请查看原文。", "citations": citations}
                if citations else insufficient("no_supporting_passage"))

    batch_id = str(UUID(batch_id))
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", question)).rstrip("?。!")
    if re.search(r"漏检|漏识别|未检出|没检出", normalized):
        return insufficient("ground_truth_required", "计算漏检数需要独立人工标注，并将标注目标与模型预测逐一匹配。")
    request = ARCHIVE_COUNT.fullmatch(normalized)
    if not request:
        return insufficient("unsupported_batch_question")
    restart = bool(request["restart"])
    if (restart and not request["final"]) or (request["continuation"] and not restart):
        return insufficient("unsupported_batch_question")
    cloud_only = any(request[key] for key in ("producer_after", "producer_before", "producer_noun"))
    if get_summary is None:
        return insufficient("batch_summary_required")
    try:
        summary = get_summary(batch_id)
    except HTTPError as exc:
        return insufficient("batch_unavailable" if exc.code in (401, 403, 404) else "dataset_unavailable")
    except (URLError, TimeoutError, ValueError):
        return insufficient("dataset_unavailable")
    if not isinstance(summary, dict):
        return insufficient("invalid_dataset_summary")
    batch = summary.get("batch")
    count = summary.get("observation_count")
    if (not isinstance(batch, dict) or batch.get("batch_id") != batch_id
            or "deleted_ms" not in batch or batch["deleted_ms"] is not None):
        return insufficient("batch_unavailable")
    if request["channel"]:
        channel = request["channel"].upper()
        selected = batch.get("channel")
        if selected not in ("A", "B", "C", "D"):
            return insufficient("batch_channel_unknown", "当前批次缺少已确认的通道信息。")
        if selected != channel:
            return insufficient("batch_channel_mismatch",
                                f"所选批次属于 {selected} 通道。请选择 {channel} 通道的批次后再查询。")
    if type(count) is not int or count < 0 or summary.get("historical_missing"):
        return insufficient("invalid_dataset_summary")
    citations = [{"id": "batch-summary", "title": "实验归档汇总",
                  "source": f"{BATCH_PREFIX}{batch_id}/summary", "section": "observation_count"}]
    facts = {"batch_id": batch_id, "observation_count": count,
             "archive_status": batch.get("archive_status"), "started_ms": batch.get("started_ms"),
             "ended_ms": batch.get("ended_ms"), "unit": "observations"}
    if restart or cloud_only:
        evidence = {d["id"]: d for d in retrieve(
            index, "D通道重启接管后的云端观测数量", limit=8, batch_id=batch_id)}
        if not all(key in evidence for key in RESTART_IDS):
            return insufficient("no_reviewed_restart_evidence")
        adoption, export = (evidence[key] for key in RESTART_IDS)
        before = adoption["facts"]["before_stop"]
        cloud = export["facts"]["cloud_observations"]
        if (batch.get("run_id") != batch_id or batch.get("channel") != "D"
                or batch.get("execution_status") != "completed" or batch.get("archive_status") != "ok"
                or summary.get("archive_error") or export["facts"]["total_observations"] != count
                or not 0 <= before <= count or not 0 <= cloud <= count):
            return insufficient("summary_disagrees_with_reviewed_evidence")
        facts.update(cloud_observations=cloud, before_stop=before, adopted_on_restart=True)
        citations += [adoption, export]
        answer = (f"该 D 通道批次最终归档 {count} 条观测，其中云端 {cloud} 条。停止服务前已归档 "
                  f"{before} 条。重启后的健康检查显示运行和批次编号仍为 {batch_id}，且报告已接管。")
    else:
        answer = f"这个批次已归档 {count} 条观测。"
        if batch.get("archive_status") != "ok" or summary.get("archive_error"):
            answer += "归档状态未确认为完整，以上是已收到并写入的记录数。"
    return {"status": "answered", "answer": answer, "facts": facts, "citations": citations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("question", nargs="?")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--batch-id")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dataset-origin")
    source.add_argument("--summary-json", type=Path, help="Replay a captured summary for offline evaluation")
    args = parser.parse_args()
    if args.build:
        build(ROOT / "knowledge/project_corpus.json", args.index,
              additional=restart_documents(ROOT.parents[1]), default_revision="c3e4f8d")
    if args.question is None:
        if not args.build:
            parser.error("question required unless building")
        return
    get_summary = None
    get_environment = None
    if args.dataset_origin:
        credential = os.environ.get("ABC_READ_CONSOLE_BASIC")
        auth = "Basic " + base64.b64encode(credential.encode()).decode() if credential else None
        get_summary = lambda batch_id: read_summary(args.dataset_origin, batch_id, auth)
        get_environment = lambda: _read_json(args.dataset_origin, ENVIRONMENT_LATEST, auth)
    elif args.summary_json:
        get_summary = lambda _: json.loads(args.summary_json.read_text(encoding="utf-8"))
    result = query(args.index, args.question, args.batch_id, get_summary, get_environment)
    result["index_sha256"] = hashlib.sha256(args.index.read_bytes()).hexdigest()
    result["data_mode"] = "captured_summary" if args.summary_json else "current_api" if args.dataset_origin else "documents"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
