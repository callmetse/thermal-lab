"""Two roles share a protocol; all heavy inference runs outside MQTT callbacks."""
import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import queue
import signal
import threading
import time
import uuid

from .common import ROOT, accepted_command, canonical_hash, config, latest_put, mqtt_client, pack, publish, unpack
from .presentation import compact_observation, next_preview_slot


def preview_packet(header, image, obs, c):
    """Preview compression is independent of the image used for inference."""
    import cv2
    side = c.get('preview_max_side', c.get('encode_max_side', 640))
    height, width = image.shape[:2]
    if max(height, width) > side:
        scale = side / max(height, width)
        image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    flags = [cv2.IMWRITE_JPEG_QUALITY, c.get('preview_jpeg_quality', c['jpeg_quality'])]
    if c.get('preview_jpeg_optimize', False):
        flags += [cv2.IMWRITE_JPEG_OPTIMIZE, 1]
    ok, encoded = cv2.imencode('.jpg', image, flags)
    if not ok:
        raise RuntimeError('Preview encoding failed')
    jpeg = encoded.tobytes()
    meta = {**header, 'preview_id': header['run_id'] + ':' + str(header['source_frame_id']),
            'preview_size': [image.shape[1], image.shape[0]],
            'preview_transport': 'pi-local' if c.get('local_preview', False) else 'cloud',
            'preview_jpeg_sha256': hashlib.sha256(jpeg).hexdigest(),
            'observation': compact_observation(obs)}
    if c.get('local_preview', False):
        # Both endpoints are this Pi's monotonic clock; excludes browser delivery.
        meta['pi_capture_to_preview_ms'] = (time.monotonic_ns() - header['frame_available_mono_ns']) / 1e6
    return pack(meta, jpeg)


class Worker:
    def __init__(self, role):
        self.c, self.role = config(), role
        self.root = self.c["topic_root"]
        self.commands, self.frames = queue.Queue(8), queue.Queue(1)
        self.stop = threading.Event()
        self.vision = self.gate = None
        self.active_run = None
        self.producer_instance_id = str(uuid.uuid4())
        self._result_journal = None
        self._result_stats = None
        self._observation_seq = 0
        self.rx_frames = self.frame_overwrites = self.foreign_frames = 0
        self.local_preview = None
        self.preview_results = queue.Queue(1)
        self.preview_cache = OrderedDict()
        self.preview_lock = threading.Lock()
        self.preview_stop = threading.Event()
        self.preview_thread = None
        self.preview_stats = {}
        self.last_preview_meta = 0
        if role == 'edge' and self.c.get('local_preview', False):
            from .local_preview import LocalPreview
            self.local_preview = LocalPreview(port=self.c.get('local_preview_port', 28990)).start()
        self.client = mqtt_client(self.c, role, self.receive)
        self.media_client = None
        self.media_c = {**self.c, 'mqtt_host': self.c.get('media_host', '127.0.0.1'),
                        'mqtt_port': self.c.get('media_port', 28583),
                        'mqtt_inflight': max(1, self.c.get('media_ack_window', 0))}
        if not self.c.get('media_enabled', False):
            self.media_c.update(mqtt_host=self.c['mqtt_host'], mqtt_port=self.c['mqtt_port'])
        def connected(client, userdata, flags, reason, properties):
            client.subscribe(self.root + "/commands", qos=1)
            if self.local_preview:
                client.subscribe(self.root + '/runs/+/observations', qos=1)
        self.client.on_connect = connected
        # Covers the initial connection if CONNACK raced the callback assignment.
        connected(self.client, None, None, None, None)
        if role == 'cloud':
            self.media()

    def media(self):
        if self.media_client is None:
            self.media_client = mqtt_client(self.media_c, self.role + '-media', self.receive)
            self.media_client.abc_frame_ack_window = self.c.get('media_ack_window', 0)
            if self.role == 'cloud':
                def connected(client, userdata, flags, reason, properties):
                    client.subscribe(self.root + '/runs/+/frames', qos=1)
                self.media_client.on_connect = connected
                connected(self.media_client, None, None, None, None)
        return self.media_client

    def event(self, run, kind, **kw):
        publish(self.client, self.root + f'/runs/{run["run_id"]}/events', {
            "project": self.c["project"], "run_id": run["run_id"], "arm": run["arm"],
            "producer": self.role, "kind": kind, **kw}, qos=1)

    def publish_preview(self, run, packet):
        if not self.local_preview:
            return publish(self.client, self.root + f'/runs/{run["run_id"]}/preview', packet)
        if not self.local_preview.publish(packet):
            return 0
        self.preview_stats['display_packets'] += 1
        if time.monotonic() - self.last_preview_meta >= .5:
            meta, _ = unpack(packet, self.c['project'])
            keys = ('project', 'run_id', 'arm', 'source_frame_id', 'source_pts_s', 'preview_id',
                    'encoded_size', 'preview_size', 'jpeg_sha256', 'preview_jpeg_sha256')
            publish(self.client, self.root + f'/runs/{run["run_id"]}/preview_meta',
                    {**{k: meta[k] for k in keys if k in meta}, 'preview_transport': 'pi-local'})
            self.last_preview_meta = time.monotonic()
        return len(packet)

    def cache_preview_source(self, header, jpeg):
        if self.local_preview:
            with self.preview_lock:
                self.preview_cache[header['source_frame_id']] = (header, jpeg)
                while len(self.preview_cache) > self.c.get('local_preview_cache_frames', 120):
                    self.preview_cache.popitem(last=False)

    def preview_loop(self, run):
        import cv2
        import numpy as np
        last_slot, last_frame = -1e9, -1
        while not self.preview_stop.is_set() and not self.stop.is_set():
            try:
                obs = self.preview_results.get(timeout=.1)
            except queue.Empty:
                continue
            frame = obs.get('source_frame_id', -1)
            if obs.get('run_id') != run['run_id'] or frame <= last_frame:
                continue
            with self.preview_lock:
                source = self.preview_cache.get(frame)
            if not source:
                self.preview_stats['cache_misses'] += 1
                continue
            header, jpeg = source
            if obs.get('jpeg_sha256') != header['jpeg_sha256'] or obs.get('arm') != run['arm']:
                self.preview_stats['mismatches'] += 1
                continue
            slot = next_preview_slot(header['source_pts_s'], self.c['preview_hz'], last_slot)
            if slot is None:
                continue
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            try:
                self.publish_preview(run, preview_packet(header, image, obs, self.c))
            except Exception as error:
                self.event(run, 'preview_failed', error=str(error))
                continue
            last_slot, last_frame = slot, frame

    def _begin_results(self):
        self._observation_seq = 0
        self._result_journal = None
        self._result_stats = {
            "generated": 0, "accepted_for_publish": 0, "publish_rejected": 0,
            "raw_journal_bytes": 0, "journal_path": None,
            "journal_enabled": bool(self.c.get("result_journal", False)),
            "qos": self.c.get("result_qos", 0),
        }
        if type(self._result_stats["qos"]) is not int or self._result_stats["qos"] not in (0, 1, 2):
            raise ValueError("result_qos must be 0, 1 or 2")

    def publish_observation(self, run, out):
        """Journal the full generated result before attempting network publication."""
        if self._result_stats is None:
            raise RuntimeError("Result session was not initialized")
        if out.get("run_id") != run["run_id"]:
            raise ValueError("Observation does not match the active result session")
        self._observation_seq += 1
        self._result_stats["generated"] += 1
        out["producer_instance_id"] = self.producer_instance_id
        out["observation_seq"] = self._observation_seq
        if self._result_stats["journal_enabled"]:
            if self._result_journal is None:
                relative = Path("data/results") / run["run_id"] / (self.role + "-" + self.producer_instance_id + ".jsonl")
                self._result_stats["journal_path"] = relative.as_posix()
                path = ROOT / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                self._result_journal = path.open("ab")
            record = {"channel": "observations", "producer_recorded_wall_ms": time.time() * 1000,
                      "payload": out}
            encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            self._result_journal.write(encoded)
            # Flush each record to the OS; this is not an fsync/power-loss guarantee.
            self._result_journal.flush()
            self._result_stats["raw_journal_bytes"] += len(encoded)
        sent = publish(self.client, self.root + f'/runs/{run["run_id"]}/observations', out,
                       qos=self._result_stats["qos"])
        key = "accepted_for_publish" if sent else "publish_rejected"
        self._result_stats[key] += 1
        return sent

    def _close_results(self):
        journal, self._result_journal = self._result_journal, None
        if journal is not None:
            journal.close()

    def receive(self, client, userdata, msg):
        try:
            if msg.topic == self.root + "/commands":
                body = json.loads(msg.payload)
                if not msg.retain and accepted_command(body, self.c):
                    try:
                        self.commands.put_nowait(body)
                    except queue.Full:
                        pass
            elif self.local_preview and msg.topic.endswith('/observations'):
                from .result_codec import decode_json
                obs = decode_json(msg.payload)
                if (obs.get('project') == self.c['project'] and obs.get('run_id') == self.active_run
                        and msg.topic == self.root + '/runs/' + str(self.active_run) + '/observations'
                        and obs.get('producer') == 'cloud'):
                    latest_put(self.preview_results, obs)
            elif self.role == "cloud" and msg.topic.endswith('/frames'):
                h, jpeg = unpack(msg.payload, self.c["project"])
                if h["run_id"] != self.active_run or msg.topic.split("/")[-2] != h["run_id"]:
                    self.foreign_frames += 1
                    return
                self.rx_frames += 1
                self.frame_overwrites += latest_put(self.frames, (h, jpeg, time.monotonic_ns()))
        except (ValueError, KeyError, TypeError):
            pass

    def run(self):
        active = None
        cancel = threading.Event()
        current = None
        completed = set()
        while not self.stop.is_set():
            try:
                cmd = self.commands.get(timeout=.2)
            except queue.Empty:
                continue
            if cmd["op"] == "stop":
                if active == cmd["run_id"]:
                    cancel.set()
                continue
            if cmd["run_id"] in completed or (current and current.is_alive()):
                continue
            active = cmd["run_id"]
            self.active_run = active
            completed.add(active)
            if len(completed) > 1000:
                completed = {active}
            cancel = threading.Event()
            while not self.frames.empty():
                self.frames.get_nowait()
            current = threading.Thread(target=self.session, args=(cmd, cancel))
            current.start()
        cancel.set()
        if current:
            current.join(timeout=10)
        self.client.disconnect()
        self.client.loop_stop()
        if self.media_client:
            self.media_client.disconnect()
            self.media_client.loop_stop()
        if self.local_preview:
            self.local_preview.close()

    def session(self, run, cancel):
        try:
            self._begin_results()
            if self.local_preview:
                self.preview_stats = {'display_packets': 0, 'cache_misses': 0, 'mismatches': 0}
                self.preview_stop.clear()
                self.last_preview_meta = 0
                self.local_preview.set_run(run['run_id'])
                if run['arm'] != 'A':
                    self.preview_thread = threading.Thread(target=self.preview_loop, args=(run,), daemon=True)
                    self.preview_thread.start()
            if self.role == "edge":
                self.edge(run, cancel)
            elif run["arm"] != "A":
                self.cloud(run, cancel)
        except Exception as e:
            self.event(run, "failed", error=f"{type(e).__name__}: {e}")
        finally:
            if self.local_preview:
                self.preview_stop.set()
                if self.preview_thread:
                    self.preview_thread.join(timeout=6)
                    self.preview_thread = None
                self.local_preview.set_run(None)
                with self.preview_lock:
                    self.preview_cache.clear()
                while not self.preview_results.empty():
                    self.preview_results.get_nowait()
                self.event(run, 'local_preview_stats', **self.preview_stats)
            try:
                self._close_results()
            except Exception as e:
                self.event(run, "failed", error=f"Result journal close failed: {type(e).__name__}: {e}")
            if self._result_stats is not None:
                self.event(run, "result_stats", producer_instance_id=self.producer_instance_id,
                           **self._result_stats)
            self.event(run, "role_finished")
            if self.active_run == run["run_id"]:
                self.active_run = None

    def load_vision(self, run):
        from .vision import Vision
        begin = time.monotonic()
        if self.vision is None:
            self.vision = Vision(self.c, edge=self.role == "edge")
        self.vision.reset()
        self.event(run, "vision_ready", load_ms=(time.monotonic() - begin) * 1000,
                   config_hash=canonical_hash(self.c), backend="ncnn" if self.role == "edge" else "pytorch-cuda",
                   assets_manifest_sha256=hashlib.sha256((ROOT / "PROVENANCE.json").read_bytes()).hexdigest(),
                   pose_shape=[self.c["pose_imgsz"]] * 2, clothing_shape=[512, 512], rect=False,
                   person_selection={k: self.c[k] for k in
                                     ("max_people", "min_person_confidence", "min_person_height_ratio", "pose_max_det")})

    def edge(self, run, cancel):
        import cv2
        from .vision import observation
        c, arm = {**self.c, 'sample_fps': run.get('sample_fps', self.c['sample_fps'])}, run["arm"]
        if run["source"] == "camera" and not c["allow_camera"]:
            raise RuntimeError("Camera disabled: use replay or a separately assigned camera")
        if arm == "A":
            self.load_vision(run)
        if arm == "B" and self.gate is None:
            from ultralytics import YOLO
            gate_path = ROOT / "assets/yolo11n_ncnn_model"
            if not gate_path.exists():
                raise RuntimeError("Export the person gate to NCNN with tools/export_gate.py first")
            self.gate = YOLO(str(gate_path))
        replay = run["source"] == "replay"
        source = ROOT / c["replay_file"] if replay else c["camera_index"]
        source_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest() if replay else None
        if not replay and c.get("camera_backend") == "picamera2":
            from .camera import PicameraCapture
            cap = PicameraCapture(c)
        else:
            cap = cv2.VideoCapture(str(source) if replay else source)
        if not cap.isOpened():
            raise RuntimeError("Cannot open configured source")
        if not replay:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, c["width"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c["height"])
            cap.set(cv2.CAP_PROP_FPS, c["capture_fps"])
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        source_fps = cap.get(cv2.CAP_PROP_FPS) if replay else c["capture_fps"]
        if source_fps <= 0:
            cap.release()
            raise ValueError("Source FPS unavailable")
        raw = queue.Queue(1)
        stopped = threading.Event()
        start = time.monotonic()
        stats = {"captured": 0, "sampled": 0, "capture_overwrites": 0, "gate_positive": 0,
                 "frame_bytes": 0, "observation_bytes": 0, "preview_bytes": 0, "publish_failures": 0}
        self.event(run, "source_open", source_sha256=source_hash, config_hash=canonical_hash(c),
                   source=run['source'], capture_backend='opencv' if replay else c.get('camera_backend', 'opencv'),
                   requested_capture_fps=source_fps,
                   requested_sample_fps=c['sample_fps'],
                   requested_size=[c['width'], c['height']] if not replay else None)
        def capture():
            i = 0
            try:
                while not cancel.is_set() and not self.stop.is_set() and time.monotonic() - start < run["seconds"]:
                    if replay:
                        if cancel.wait(max(0, start + i / source_fps - time.monotonic())):
                            break
                    ok, image = cap.read()
                    if not ok:
                        break
                    ready = time.monotonic_ns()
                    pts = i / source_fps if replay else (ready / 1e9 - start)
                    stats["capture_overwrites"] += latest_put(raw, (i, pts, image, ready, time.time() * 1000))
                    stats["captured"] += 1
                    i += 1
            finally:
                cap.release()
                stopped.set()
        thread = threading.Thread(target=capture, daemon=True)
        thread.start()
        last_sample = last_preview = -1e9
        try:
            while not cancel.is_set() and not self.stop.is_set():
                try:
                    index, pts, image, ready, ready_wall = raw.get(timeout=.1)
                except queue.Empty:
                    if stopped.is_set():
                        break
                    continue
                # Source-time sampling is identical for A/B/C; processing may cause explicitly counted loss.
                slot = int(pts * c["sample_fps"] + 1e-6)
                if slot <= last_sample:
                    continue
                last_sample = slot
                stats["sampled"] += 1
                size = max(image.shape[:2])
                limit = c.get("encode_max_side", 640)
                if size > limit:
                    scale = limit / size
                    image = cv2.resize(image, (max(1, round(image.shape[1] * scale)),
                                               max(1, round(image.shape[0] * scale))), interpolation=cv2.INTER_AREA)
                # Same JPEG is decoded before A/B inference and transmitted by B/C.
                flags = [cv2.IMWRITE_JPEG_QUALITY, c["jpeg_quality"]]
                if c.get('jpeg_optimize', False):
                    flags += [cv2.IMWRITE_JPEG_OPTIMIZE, 1]
                ok, buf = cv2.imencode(".jpg", image, flags)
                if not ok:
                    continue
                jpeg = buf.tobytes()
                decoded = cv2.imdecode(buf, cv2.IMREAD_COLOR) if arm != "C" else None
                header = {"project": c["project"], "run_id": run["run_id"], "arm": arm,
                          "source": run["source"], "source_frame_id": index, "source_pts_s": pts,
                          "frame_available_mono_ns": ready, "frame_available_wall_ms": ready_wall,
                          "capture_time_basis": "picamera2_capture_array_complete" if not replay and c.get('camera_backend') == 'picamera2' else "opencv_read_complete", "source_sha256": source_hash,
                          "jpeg_sha256": hashlib.sha256(jpeg).hexdigest()}
                header["encoded_size"] = [image.shape[1], image.shape[0]]
                header["jpeg_quality"] = c["jpeg_quality"]
                header['jpeg_optimize'] = c.get('jpeg_optimize', False)
                if arm == "B":
                    gate_start = time.perf_counter_ns()
                    r = self.gate.predict(decoded, imgsz=c["gate_imgsz"], classes=[0],
                                          conf=.25, device="cpu", rect=False, verbose=False)[0]
                    header["gate_ms"] = (time.perf_counter_ns() - gate_start) / 1e6
                    if not len(r.boxes):
                        self.event(run, "gate_negative", source_frame_id=index, source_pts_s=pts,
                                   gate_ms=header["gate_ms"])
                        preview_slot = next_preview_slot(pts, c['preview_hz'], last_preview)
                        if preview_slot is not None:
                            gate_obs = observation(header, [], header['gate_ms'], 'edge-gate')
                            gate_obs['status'] = 'person_gate_negative_not_cloud_verified'
                            stats['preview_bytes'] += self.publish_preview(run, preview_packet(header, decoded, gate_obs, c))
                            last_preview = preview_slot
                        continue
                    stats["gate_positive"] += 1
                if arm == "A":
                    tracks, ms = self.vision.infer(decoded, pts)
                    if cancel.is_set():
                        break
                    out = observation(header, tracks, ms, "edge")
                    stats["observation_bytes"] += self.publish_observation(run, out)
                else:
                    self.cache_preview_source(header, jpeg)
                    sent = publish(self.media(), self.root + f'/runs/{run["run_id"]}/frames', pack(header, jpeg))
                    stats["frame_bytes"] += sent
                    stats["publish_failures"] += not bool(sent)
                preview_slot = next_preview_slot(pts, c['preview_hz'], last_preview)
                if arm == "A" and preview_slot is not None:
                    stats["preview_bytes"] += self.publish_preview(run, preview_packet(header, decoded, out, c))
                    last_preview = preview_slot
        finally:
            cancel.set()
            thread.join(timeout=3)
            self.event(run, "transport_stats", duration_s=time.monotonic() - start,
                       media_route='direct_gpu' if c.get('media_enabled') else 'control_cloud', **stats)

    def cloud(self, run, cancel):
        import cv2
        import numpy as np
        from .vision import observation
        self.load_vision(run)
        received_start, overwritten_start = self.rx_frames, self.frame_overwrites
        processed = 0
        deadline = time.monotonic() + run["seconds"] + 10
        last_id = -1
        last_preview = -1e9
        while not cancel.is_set() and not self.stop.is_set() and time.monotonic() < deadline:
            try:
                h, jpeg, received = self.frames.get(timeout=.2)
            except queue.Empty:
                continue
            if h["run_id"] != run["run_id"] or h["arm"] != run["arm"] or h["source_frame_id"] <= last_id:
                continue
            last_id = h["source_frame_id"]
            queue_ms = (time.monotonic_ns() - received) / 1e6
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                self.event(run, "decode_failed", source_frame_id=last_id)
                continue
            tracks, ms = self.vision.infer(image, h["source_pts_s"])
            processed += 1
            if cancel.is_set():
                break
            out = observation(h, tracks, ms, "cloud", {"cloud_queue_ms": queue_ms})
            self.publish_observation(run, out)
            preview_slot = next_preview_slot(h['source_pts_s'], self.c['preview_hz'], last_preview)
            if preview_slot is not None and not self.c.get('local_preview', False):
                publish(self.client, self.root + f'/runs/{run["run_id"]}/preview',
                        preview_packet(h, image, out, self.c))
                last_preview = preview_slot
        self.event(run, "cloud_stats", received=self.rx_frames - received_start,
                   slot_overwrites=self.frame_overwrites - overwritten_start, processed=processed,
                   foreign_frames_total=self.foreign_frames)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["edge", "cloud"])
    args = parser.parse_args()
    # A per-project OS lock blocks duplicate consumers without touching old locks.
    import fcntl
    path = ROOT / "state" / (args.role + ".lock")
    path.parent.mkdir(exist_ok=True)
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(args.role)
        signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
        signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
        worker.run()


if __name__ == "__main__":
    main()
