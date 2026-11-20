import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import struct
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
# Ultralytics otherwise persists settings in the account's shared config path.
for variable, subdir in [("YOLO_CONFIG_DIR", "ultralytics"), ("TORCH_HOME", "torch"),
                         ("CUDA_CACHE_PATH", "cuda-cache"), ("MPLCONFIGDIR", "matplotlib")]:
    directory = ROOT / "state" / subdir
    directory.mkdir(parents=True, exist_ok=True)
    os.environ[variable] = str(directory)
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ["YOLO_OFFLINE"] = "true"
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def config():
    path = Path(os.environ.get("ABC_CONFIG", ROOT / "config.json"))
    c = json.loads(path.read_text())
    for key, env in [("mqtt_host", "ABC_MQTT_HOST"), ("mqtt_port", "ABC_MQTT_PORT"),
                     ("llm_url", "ABC_LLM_URL")]:
        if env in os.environ:
            c[key] = int(os.environ[env]) if key.endswith("port") else os.environ[env]
    if not IDENTIFIER.fullmatch(c["project"]) or c["topic_root"] != f'labs/{c["project"]}/v1':
        raise ValueError("Use the independent labs/<project>/v1 namespace")
    if c["mqtt_port"] in (1883, 11883):
        raise ValueError("Existing project broker ports are forbidden")
    if c["llm_model"] != "qwen2.5:7b":
        raise ValueError("The common comparison requires the pinned 7B model")
    if c["sample_fps"] <= 0 or c["capture_fps"] <= 0:
        raise ValueError("FPS must be positive")
    if c.get('result_compression', 'none') not in ('none', 'gzip') or c.get('result_qos', 0) not in (0, 1, 2):
        raise ValueError('Unsupported result compression or QoS')
    if c.get('live_state_hz', 0) < 0:
        raise ValueError('UI state frequency must be nonnegative')
    if (not isinstance(c["max_people"], int) or c["max_people"] < 0
            or not isinstance(c["pose_max_det"], int) or c["pose_max_det"] < 1
            or not 0 <= c["min_person_confidence"] <= 1
            or not 0 <= c["min_person_height_ratio"] <= 1):
        raise ValueError("Invalid configurable person selection")
    return c


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def latest_put(q, value):
    try:
        q.put_nowait(value)
        return False
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(value)
        except queue.Full:
            pass
        return True


def pack(header, jpeg):
    h = json.dumps(header, separators=(",", ":")).encode()
    if len(h) > 65536 or len(jpeg) > 8_000_000:
        raise ValueError("Frame exceeds experiment limits")
    return struct.pack("!I", len(h)) + h + jpeg


def unpack(payload, project):
    if len(payload) < 6:
        raise ValueError("Truncated frame")
    size = struct.unpack("!I", payload[:4])[0]
    if size > 65536 or 4 + size >= len(payload):
        raise ValueError("Invalid frame header")
    h = json.loads(payload[4:4 + size])
    if h.get("project") != project or not IDENTIFIER.fullmatch(h.get("run_id", "")):
        raise ValueError("Foreign experiment frame")
    return h, payload[4 + size:]


def accepted_command(body, c):
    if body.get("project") != c["project"] or body.get("op") not in ("start", "stop"):
        return False
    if not IDENTIFIER.fullmatch(str(body.get("run_id", ""))):
        return False
    if body["op"] == "start":
        # Wall time is used only for rejecting old control commands, never latency measurement.
        if abs(time.time() * 1000 - body.get("issued_ms", 0)) > 120000:
            return False
        if body.get("arm") not in ("A", "B", "C") or body.get("source") not in ("camera", "replay"):
            return False
        if body.get('upload_id') is not None:
            if body.get('source') != 'replay' or not __import__('re').fullmatch(r'[a-f0-9]{32}', str(body['upload_id'])):return False
        unlimited=body.get('unlimited',False)
        if type(unlimited) is not bool:
            return False
        seconds=body.get('seconds',0)
        if type(seconds) not in (int,float) or not __import__('math').isfinite(seconds):
            return False
        if unlimited:
            if body.get('source') != 'camera' or seconds != 0:return False
        elif not 1 <= seconds <= 3600:return False
        try:
            from .recognition_region import settings
            settings(body)
        except (ValueError,TypeError):
            return False
        for field in ('collection_location','collection_name'):
            if field in body and (not isinstance(body[field],str) or len(body[field])>160):return False
        for field in ('collection_batch_id','source_zone_id'):
            if field in body and not IDENTIFIER.fullmatch(str(body[field])):return False
        rate = body.get('sample_fps', c.get('sample_fps', 12))
        if type(rate) not in (int, float) or not 1 <= rate <= c.get('capture_fps', 30):
            return False
    return True


def mqtt_client(c, role, on_message):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f'{c["project"]}-{role}-{uuid.uuid4().hex[:8]}', protocol=mqtt.MQTTv5)
    user = os.environ.get("ABC_MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ["ABC_MQTT_PASSWORD"])
    client.max_inflight_messages_set(max(c.get('mqtt_inflight', 1),
                                        c.get('result_inflight', 8) if c.get('result_qos', 0) > 0 else 1))
    client.max_queued_messages_set(8)
    client.reconnect_delay_set(1, 10)
    client.abc_result_compression = c.get('result_compression', 'none')
    client.abc_preview_ack_window = c.get('preview_ack_window', 0)
    def socket_opened(client, userdata, sock):
        if c.get('mqtt_tcp_nodelay', False):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if c.get('mqtt_send_buffer_bytes', 0) > 0:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, c['mqtt_send_buffer_bytes'])
    client.on_socket_open = socket_opened
    client.on_message = on_message
    client.connect(c["mqtt_host"], c["mqtt_port"], keepalive=20)
    client.loop_start()
    return client


def publish(client, topic, body, qos=0):
    channel = topic.rsplit("/", 1)[-1]
    if channel == 'observations' and not isinstance(body, bytes):
        from .result_codec import encode_json
        payload = encode_json(body, compress=getattr(client, 'abc_result_compression', 'none') == 'gzip')
    else:
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    if not client.is_connected():
        return 0  # Do not build a replay backlog while disconnected.
    # Paho's max_queued_messages does not bound QoS0 socket packets. Keep at most
    # one uncompleted QoS0 publish per channel using its public message-info API.
    confirmed = getattr(client, 'abc_frame_ack_window', 0) if channel == 'frames' else getattr(client, 'abc_preview_ack_window', 0) if channel == 'preview' else 0
    pending_name = 'abc_frame_pending' if channel == 'frames' else 'abc_preview_pending'
    if confirmed:
        # Remote broker PUBACK bounds frames/previews, unlike QoS0 local socket completion.
        # Drop the current candidate when the bounded window is full; no stale app queue.
        pending_frames = [info for info in getattr(client, pending_name, []) if not info.is_published()]
        setattr(client, pending_name, pending_frames)
        if len(pending_frames) >= confirmed:
            return 0
        qos = 1
    slots = getattr(client, "abc_pending_qos0", {})
    pending = slots.get(channel)
    if qos == 0 and pending is not None and not pending.is_published():
        return 0
    kw = {}
    if channel in ("frames", "preview"):
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes
        properties = Properties(PacketTypes.PUBLISH)
        properties.MessageExpiryInterval = 2
        kw["properties"] = properties
    result = client.publish(topic, payload, qos=qos, retain=False, **kw)
    if confirmed and result.rc == 0:
        getattr(client, pending_name).append(result)
    if qos == 0 and result.rc == 0:
        slots[channel] = result
        client.abc_pending_qos0 = slots
    return len(payload) if result.rc == 0 else 0
