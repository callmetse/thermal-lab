"""A 线路端到端检查：发一条 A/仅检测 的 start，收观测与 crop_attrs。

在 Pi 上运行（用 127.0.0.1:28883 → 控制云 broker 的隧道）。
只读统计，不写任何实验数据；会话结束后自动 stop。
"""
import json
import sys
import time
import uuid

import paho.mqtt.client as mqtt

PROJECT = "abc20261102"
ROOT = f"labs/{PROJECT}/v1"
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
CLASSES = [int(v) for v in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["0"])]

run_id = "a-line-" + uuid.uuid4().hex[:8]
obs = {"edge_object": 0, "cloud": 0, "tracks": 0}
attrs = []
events = []


def on_message(client, userdata, msg):
    try:
        if msg.topic.endswith("/observations"):
            body = json.loads(msg.payload)
            producer = body.get("producer")
            if body.get("run_id") != run_id:
                return
            if producer == "edge-object":
                obs["edge_object"] += 1
                obs["tracks"] += len(body.get("tracks") or [])
            elif producer == "cloud":
                obs["cloud"] += 1
        elif msg.topic.endswith("/crop_attrs"):
            body = json.loads(msg.payload)
            if body.get("run_id") == run_id:
                attrs.append(body)
        elif msg.topic.endswith("/events"):
            body = json.loads(msg.payload)
            if body.get("run_id") == run_id and body.get("kind") in ("crop_stats", "crop_infer_failed"):
                events.append(body)
    except (ValueError, KeyError):
        pass


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-a-line-{uuid.uuid4().hex[:6]}")
client.on_message = on_message
client.connect("127.0.0.1", 28883, 20)
client.subscribe(ROOT + "/runs/+/observations", qos=0)
client.subscribe(ROOT + "/runs/+/crop_attrs", qos=0)
client.subscribe(ROOT + "/runs/+/events", qos=0)
client.loop_start()
time.sleep(1.0)

start = {"project": PROJECT, "op": "start", "run_id": run_id, "arm": "A", "source": "replay",
         "seconds": SECONDS, "sample_fps": 12, "confidence_min": 0.25, "confidence_max": 1.0,
         "recognition_region": {"mode": "full"}, "collection_location": "A线路验收",
         "collection_name": "仅物体检测+裁剪上云", "object_classes": CLASSES,
         "arm_a_mode": "detect"}
client.publish(ROOT + "/commands", json.dumps(start, ensure_ascii=False), qos=1)
print("start sent", run_id, "classes", CLASSES, flush=True)
time.sleep(SECONDS + 6)
client.publish(ROOT + "/commands", json.dumps({"project": PROJECT, "op": "stop", "run_id": run_id}), qos=1)
time.sleep(2)
client.loop_stop()

slots = sum(len(a.get("attrs") or []) for a in attrs)
closed = [a for a in attrs if any((x.get("posture") or {}).get("state") not in (None, "pending_cloud") for x in a.get("attrs") or [])]
classes = {}
for a in attrs:
    for x in a.get("attrs") or []:
        classes[x.get("class_name")] = classes.get(x.get("class_name"), 0) + 1
print(json.dumps({"run_id": run_id, "seconds": SECONDS,
                  "edge_observations": obs["edge_object"], "tracks_total": obs["tracks"],
                  "cloud_observations": obs["cloud"],
                  "crop_attr_messages": len(attrs), "crop_attr_slots": slots,
                  "with_posture": len(closed), "class_counts": classes,
                  "events": events[:6]}, ensure_ascii=False))
