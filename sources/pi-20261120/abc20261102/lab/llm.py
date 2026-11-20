"""Async 7B reasoning through an independent Ollama instance."""
import json
import hashlib
import queue
import signal
import threading
import time
import urllib.request

from .common import config, latest_put, mqtt_client, publish
from .result_codec import decode_json

SYSTEM = """你是热舒适实验的事实解释器。用中文返回JSON，仅含summary和uncertainty。
只解释提供的人数、可见衣物、姿态和动作候选。unknown必须保持未知。
clothing_detection_status为unknown_no_detection只表示衣物信息未知。必须写“衣物未检出”或“衣物信息未知”。
禁止说无穿着、未穿衣、没有衣服、未见衣物或裸露；未检出不表示不存在。
不得把躺着说成睡眠，不得将坐姿或站姿当作任务或代谢强度。
不得推断隐藏衣层、材料、性别、身份、脸或步态身份；不得编造clo/met数值。
可以报告输入轨迹数量，不得当作整个画面真实人数。summary最多60汉字，uncertainty最多40汉字。"""

OUTPUT_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"}, "uncertainty": {"type": "string"}},
    "required": ["summary", "uncertainty"], "additionalProperties": False}


def validate_reply(draft):
    schema_ok = (isinstance(draft, dict) and set(draft) == {"summary", "uncertainty"}
                 and all(isinstance(draft.get(k), str) for k in ("summary", "uncertainty")))
    flags = []
    if schema_ok:
        text = draft['summary'] + draft['uncertainty']
        # A narrow known-error check, not a general semantic-accuracy guarantee.
        if any(phrase in text for phrase in ("无穿着", "未穿着衣物", "未穿衣", "没有穿衣", "没有衣服", "未见衣物", "裸露", "裸体")):
            flags.append('unsupported_clothing_absence_claim')
    return schema_ok, flags


def request_json(url, payload=None, timeout=40):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def facts(obs):
    return [{"track_id": p["track_id"], "posture": p["posture"], "action": p["action"],
             "clothing": [v["category"] for v in p["clothing"]],
             "clothing_detection_status": "detected_items" if p["clothing"] else "unknown_no_detection",
             "clothing_age_s": p.get("clothing_age_s"),
             "clo": p["clo"], "met": p["met"], "estimate_status": p["estimate_status"]}
            for p in obs.get("tracks", [])]


class ObservationCursor:
    """Reject repeated/retired QoS1 results without changing their evidence."""
    def __init__(self):
        self.run_id, self.producer, self.position = None, None, -1
        self.retired = set()

    def start(self, run_id):
        if run_id != self.run_id:
            self.run_id, self.producer, self.position = run_id, None, -1
            self.retired.clear()

    @staticmethod
    def producer_key(value):
        return value.get("producer_instance_id") or "legacy:" + str(value.get("producer", "unknown"))

    def current(self, value):
        return value.get("run_id") == self.run_id and self.producer_key(value) == self.producer

    def accept(self, value):
        if value.get("run_id") != self.run_id:
            return False
        producer = self.producer_key(value)
        position = value.get("observation_seq") if value.get("producer_instance_id") else value.get("source_frame_id")
        if type(position) is not int or position < 0 or producer in self.retired:
            return False
        if producer != self.producer:
            if self.producer is not None:
                self.retired.add(self.producer)
            self.producer, self.position = producer, -1
        if position <= self.position:
            return False
        self.position = position
        return True


def main():
    c = config()
    items = queue.Queue(1)
    stop = threading.Event()
    active = {"run_id": None}
    cursor = ObservationCursor()
    def receive(client, userdata, msg):
        try:
            v = decode_json(msg.payload)
            if v.get("project") != c["project"]:
                return
            if msg.topic.endswith("/commands"):
                from .common import accepted_command
                if not msg.retain and accepted_command(v, c):
                    if v["op"] == "start":
                        cursor.start(v["run_id"])
                        active["run_id"] = v["run_id"]
                    elif active["run_id"] == v["run_id"]:
                        active["run_id"] = None
            elif v.get("run_id") == active["run_id"] and cursor.accept(v):
                latest_put(items, v)
        except (ValueError, TypeError):
            pass
    client = mqtt_client(c, "llm7b", receive)
    def connected(client, userdata, flags, reason, properties):
        client.subscribe([(c["topic_root"] + "/commands", 1),
                          (c["topic_root"] + "/runs/+/observations", 1)])
    client.on_connect = connected
    connected(client, None, None, None, None)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last = -1e9
    model_manifest = None
    try:
        while not stop.is_set():
            if time.monotonic() - last < c["llm_interval_s"]:
                stop.wait(.05)
                continue
            try:
                obs = items.get(timeout=.2)
            except queue.Empty:
                continue
            if obs["run_id"] != active["run_id"] or not cursor.current(obs):
                continue
            start = time.monotonic()
            last = start
            result = {"project": c["project"], "run_id": obs["run_id"], "arm": obs["arm"],
                      "source_frame_id": obs["source_frame_id"], "model": c["llm_model"],
                      "producer_instance_id": obs.get("producer_instance_id"),
                      "observation_seq": obs.get("observation_seq"),
                      "system_prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
                      "semantic_accuracy": "requires_human_labels"}
            try:
                if model_manifest is None:
                    tags = request_json(c["llm_url"] + "/api/tags")
                    model_manifest = next((m for m in tags.get("models", []) if m.get("name") == c["llm_model"]), None)
                    if model_manifest is None:
                        raise RuntimeError("Pinned 7B model not installed in the independent Ollama")
                result["model_manifest"] = model_manifest
                reply = request_json(c["llm_url"] + "/api/generate", {
                    "model": c["llm_model"], "system": SYSTEM,
                    "prompt": json.dumps(facts(obs), ensure_ascii=False), "format": OUTPUT_SCHEMA,
                    "stream": False, "keep_alive": "10m",
                    "options": {"temperature": 0, "seed": 42, "num_predict": c["llm_max_tokens"]}})
                draft = json.loads(reply["response"])
                schema_ok, flags = validate_reply(draft)
                accepted = schema_ok and not flags
                result.update(status="generated" if accepted else "rejected", draft=draft,
                              schema_guard_passed=schema_ok, semantic_flags=flags,
                              ollama_duration_ns=reply.get("total_duration"))
            except Exception as e:
                result.update(status="failed", error=f"{type(e).__name__}: {e}")
            result["generation_ms"] = (time.monotonic() - start) * 1000
            result["generation_timing_basis"] = "adapter_total_including_http_and_first_manifest_read"
            if obs["run_id"] == active["run_id"] and cursor.current(obs):
                publish(client, c["topic_root"] + f'/runs/{obs["run_id"]}/llm', result)
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
