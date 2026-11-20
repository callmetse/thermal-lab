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
        self.crops = queue.Queue(8)
        self.stop = threading.Event()
        self.vision = self.gate = None
        self.object_line = None
        self.crop_infer = None
        self.rx_crops = 0
        self.crop_attrs = {}
        self.crop_attrs_lock = threading.Lock()
        self.rx_attrs = 0
        self.session_state = 'idle'
        self.session_reason = None
        self.session_run = None
        self.generation = 0
        self.lease_expires = 0.0
        self.lease_ttl = float(self.c.get('lease_ttl_s', 120))
        self.session_started = None
        self.session_config = None
        self.active_run = None
        self.producer_instance_id = str(uuid.uuid4())
        self._result_journal = None
        self._result_stats = None
        self._observation_seq = 0
        self.rx_frames = self.frame_overwrites = self.foreign_frames = 0
        self.local_preview = None
        self.dataset_archive = None
        self.archive = None
        self.preview_results = queue.Queue(1)
        self.preview_cache = OrderedDict()
        self.preview_lock = threading.Lock()
        self.preview_stop = threading.Event()
        self.preview_thread = None
        self.preview_stats = {}
        self.last_preview_meta = 0
        # 边云协作策略：只有 edge 侧、且配置显式启用时才实例化。
        # 默认 enabled=false → 完全不进入判定路径，基线 A/B/C 行为不变。
        self.policy = None
        self.scene_gate = None
        self.health = None
        self.last_person_seen = None
        collaboration = self.c.get('collaboration') or {}
        if role == 'edge':
            from .gpu_health import GpuHealth
            self.health = GpuHealth(collaboration.get('health') or {})
            if collaboration.get('enabled') and collaboration.get('scene_gate_enabled'):
                from .scene_gate import SceneGate
                self.scene_gate = SceneGate(collaboration,
                                            sample_fps=self.c.get('sample_fps', 20))
        if role == 'edge' and collaboration.get('enabled'):
            from .transfer_policy import MediaBudgetPolicy
            self.policy = MediaBudgetPolicy(collaboration)
        # W1 难例采集：只写本地暂存箱，不改变推理与发送路径。
        self.hard_selector = None
        self.sample_store = None
        hard = self.c.get('hard_examples') or {}
        if role == 'edge' and hard.get('enabled'):
            from .hard_examples import HardExampleSelector
            from .sample_store import BoundedSampleStore
            self.hard_selector = HardExampleSelector(hard)
            self.sample_store = BoundedSampleStore(
                ROOT / 'state' / 'hard_examples',
                max_bytes=hard.get('max_bytes', 50 * 1024 * 1024),
                max_samples=hard.get('max_samples', 500),
                max_age_s=hard.get('max_age_s', 7 * 24 * 3600))
        if role == 'edge' and self.c.get('dataset_api') in (True, 1, 'on', 'true', 'True'):
            from .archive_ingest import ArchiveIngest
            dataset_api_url = 'http://127.0.0.1:%d' % int(self.c.get('dataset_api_port', 28992))
            self.dataset_archive = ArchiveIngest(dataset_api_url, journal=ROOT/'state/archive_sessions.sqlite3')
            self.archive = self.dataset_archive
        if role == 'edge' and self.c.get('local_preview', False):
            from .local_preview import LocalPreview
            self.local_preview = LocalPreview(port=self.c.get('local_preview_port', 28990),
                                              preview_only=self.dataset_archive is not None,
                                              archive_client=self.dataset_archive).start()
            if self.archive is None:
                self.archive = self.local_preview.archive
        self.client = mqtt_client(self.c, role, self.receive)
        self.media_client = None
        self.media_c = {**self.c, 'mqtt_host': self.c.get('media_host', '127.0.0.1'),
                        'mqtt_port': self.c.get('media_port', 28583),
                        'mqtt_inflight': max(1, self.c.get('media_ack_window', 0))}
        if not self.c.get('media_enabled', False):
            self.media_c.update(mqtt_host=self.c['mqtt_host'], mqtt_port=self.c['mqtt_port'])
        def connected(client, userdata, flags, reason, properties):
            client.subscribe(self.root + "/commands", qos=1)
            if self.local_preview or self.archive:
                client.subscribe(self.root + '/runs/+/observations', qos=1)
            if role == 'edge':
                # A 线路：云端回填的框内属性（只回属性，不回整条观测）。
                client.subscribe(self.root + '/runs/+/crop_attrs', qos=0)
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
                    client.subscribe(self.root + '/runs/+/crops', qos=0)
                self.media_client.on_connect = connected
                connected(self.media_client, None, None, None, None)
        return self.media_client

    def event(self, run, kind, **kw):
        if self.archive:
            self.archive.submit("event", {"run_id":run["run_id"],"collection_batch_id":run.get("collection_batch_id"),
                                          "kind":kind,"producer":self.role,**kw})
        publish(self.client, self.root + f'/runs/{run["run_id"]}/events', {
            "project": self.c["project"], "run_id": run["run_id"], "arm": run["arm"],
            "producer": self.role, "kind": kind, **kw}, qos=1)

    def publish_session(self, run_id, kind, **kw):
        """会话状态/心跳：复用 events 通道，避免新增 MQTT 订阅。"""
        if not run_id or self.role != 'edge':
            return 0
        return publish(self.client, self.root + f'/runs/{run_id}/events', {
            "project": self.c["project"], "run_id": run_id, "producer": self.role,
            "kind": kind, "state": self.session_state, "generation": self.generation,
            "ts_ms": int(time.time() * 1000), **kw}, qos=1)

    def set_session(self, state, reason=None):
        self.session_state = state
        self.session_reason = reason
        extra = {}
        if state == 'running' and self.session_config:
            extra['session'] = self.session_config
        self.publish_session(self.session_run, 'session_state', reason=reason, **extra)
        return state

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
            if getattr(self,'_journal_segment_bytes',0)+len(encoded)>32*1024*1024:
                journal_path=__import__('pathlib').Path(self._result_journal.name)
                self._result_journal.close()
                __import__('os').replace(str(journal_path),str(journal_path)+'.1')
                self._result_journal=journal_path.open('ab')
                self._journal_segment_bytes=0
            self._result_journal.write(encoded)
            self._journal_segment_bytes=getattr(self,"_journal_segment_bytes",0)+len(encoded)
            # Flush each record to the OS; this is not an fsync/power-loss guarantee.
            self._result_journal.flush()
            self._result_stats["raw_journal_bytes"] += len(encoded)
        if self.archive:
            self.archive.submit("observation",out)
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
                    if body.get("op") == "lease":
                        if body.get("run_id") == self.session_run and (
                                body.get("generation") in (None, self.generation)):
                            self.lease_expires = time.monotonic() + float(
                                body.get("ttl_s") or self.lease_ttl)
                        return
                    if body.get("op") == "session_query":
                        self.publish_session(self.session_run, 'session_state', reason='query')
                        return
                    try:
                        self.commands.put_nowait(body)
                    except queue.Full:
                        pass
            elif (self.local_preview or self.archive) and msg.topic.endswith('/observations'):
                from .result_codec import decode_json
                obs = decode_json(msg.payload)
                if (obs.get('project') == self.c['project'] and obs.get('run_id') == self.active_run
                        and msg.topic == self.root + '/runs/' + str(self.active_run) + '/observations'
                        and obs.get('producer') == 'cloud'):
                    if self.archive:
                        self.archive.submit("observation",obs)
                    if self.local_preview:
                        latest_put(self.preview_results, obs)
                    # 云端说过"有人"就记一笔：帧差门控不能只凭画面安静就判定空场景，
                    # 静坐的人几乎不动，差分会把它们当空场景（实测 70% 帧误判）。
                    if obs.get('tracks'):
                        self.last_person_seen = time.monotonic()
                    selector = getattr(self, 'hard_selector', None)
                    if selector is not None:
                        selector.note_observation(len(obs.get('tracks') or []))
                    scene_gate = getattr(self, 'scene_gate', None)
                    if scene_gate is not None:
                        # 云端检测器的判断才是"有没有人"的证据；帧差只能当提示。
                        if obs.get('tracks'):
                            scene_gate.note_evidence('PRESENT', epoch=obs.get('run_id'))
                        elif obs.get('status') == 'model_empty_candidate':
                            scene_gate.note_evidence('EMPTY', epoch=obs.get('run_id'))
                    health = getattr(self, 'health', None)
                    if health is not None:
                        # 应用级就绪的唯一证据：真的收到了本会话的云端结果。
                        timing = obs.get('timing') or {}
                        health.note_observation(queue_ms=timing.get('cloud_queue_ms'),
                                                infer_ms=obs.get('infer_ms'),
                                                status=obs.get('status'))
            elif msg.topic.endswith('/crop_attrs'):
                body = json.loads(msg.payload)
                self.rx_attrs += 1
                if body.get('run_id') == self.active_run:
                    with self.crop_attrs_lock:
                        for item in body.get('attrs') or []:
                            if item.get('track_id') is not None:
                                # at 必须用本机 monotonic：云端的时间戳跨机器不可比。
                                self.crop_attrs[item['track_id']] = {**item, 'at': time.monotonic()}
            elif self.role == "cloud" and msg.topic.endswith('/crops'):
                h, jpeg = unpack(msg.payload, self.c["project"])
                if (h.get('kind') != 'crop_batch' or h["run_id"] != self.active_run
                        or msg.topic.split("/")[-2] != h["run_id"]):
                    return
                self.rx_crops += 1
                latest_put(self.crops, (h, jpeg))
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
        pending_cloud_start = None
        completed = set()
        while not self.stop.is_set():
            pending_ready = pending_cloud_start is not None and not current.is_alive()
            try:
                cmd = self.commands.get(timeout=0 if pending_ready else .2)
            except queue.Empty:
                if not pending_ready:
                    continue
                cmd, pending_cloud_start = pending_cloud_start, None
            if cmd["op"] == "stop":
                if (pending_cloud_start is not None and cmd["run_id"] == pending_cloud_start["run_id"]
                        and cmd.get("generation") in (None, pending_cloud_start.get("generation"))):
                    pending_cloud_start = None
                if active == cmd["run_id"] and (cmd.get("generation") in (None, self.generation)):
                    self.set_session('stopping')
                    cancel.set()
                continue
            if cmd["run_id"] in completed:
                continue
            if pending_cloud_start is not None or (current and current.is_alive()):
                generation = cmd.get("generation")
                pending_generation = (pending_cloud_start or {}).get("generation", self.generation)
                if self.role == "cloud" and type(generation) is int and generation > pending_generation:
                    # The edge can finish replay before the cloud has drained the prior run.
                    # Retain the newer start while the cloud worker closes that run.
                    pending_cloud_start = cmd
                    cancel.set()
                continue
            active = cmd["run_id"]
            self.generation = cmd.get("generation", self.generation)
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
        if self.dataset_archive:
            self.dataset_archive.close()

    def lease_guard(self, run, cancel):
        """租约到期就停机：断链超过 lease_ttl_s 不允许继续跑。"""
        while not cancel.is_set() and not self.stop.is_set():
            if time.monotonic() > self.lease_expires:
                self.set_session('interrupted', reason='lease_expired')
                cancel.set()
                return
            time.sleep(.5)

    def heartbeat_loop(self, run, cancel):
        while not cancel.is_set() and not self.stop.is_set():
            self.publish_session(run['run_id'], 'heartbeat',
                                 uptime_s=round(time.monotonic() - (self.session_started or time.monotonic()), 1))
            time.sleep(2.0)

    def session(self, run, cancel):
        try:
            self.session_run = run['run_id']
            self.session_config = {k: run.get(k) for k in (
                'run_id', 'arm', 'source', 'seconds', 'unlimited', 'generation',
                'collection_location', 'collection_name', 'source_zone_id', 'upload_id',
                'ui_channel', 'sample_fps', 'confidence_min', 'confidence_max',
                'recognition_region') if k in run}
            self.generation = run.get('generation', self.generation)
            self.session_started = time.monotonic()
            if not self.lease_expires:
                self.lease_expires = self.session_started + self.lease_ttl
            self.lease_expires = max(self.lease_expires, self.session_started + self.lease_ttl)
            self.set_session('starting')
            threading.Thread(target=self.lease_guard, args=(run, cancel), daemon=True).start()
            threading.Thread(target=self.heartbeat_loop, args=(run, cancel), daemon=True).start()
            self._begin_results()
            if self.archive:
                run = dict(run)
                run.setdefault('model_manifest', self.model_manifest())
                keys = ('sample_fps','capture_fps','width','height','encode_max_side','jpeg_quality','replay_file',
                        'llm_model','min_person_confidence','max_people','pose_max_det','pose_imgsz','gate_imgsz',
                        'rect','camera_backend','collaboration','hard_examples')
                run['effective_settings'] = {key: run.get(key, self.c.get(key)) for key in keys}
            if self.dataset_archive:
                self.dataset_archive.start_session(run)
            self.set_session('running')
            if self.local_preview:
                self.preview_stats = {'display_packets': 0, 'cache_misses': 0, 'mismatches': 0,
                                      'fallback_packets': 0}
                self.preview_stop.clear()
                self.last_preview_meta = 0
                self.local_preview.set_run(run['run_id'],run)
                if run['arm'] != 'A':
                    self.preview_thread = threading.Thread(target=self.preview_loop, args=(run,), daemon=True)
                    self.preview_thread.start()
            if self.role == "edge":
                self.edge(run, cancel)
            elif run["arm"] != "A":
                self.cloud(run, cancel)
            elif run.get('arm_a_mode', self.c.get('a_mode', 'detect')) == 'detect':
                # A 线路：云端不接整帧，只处理裁剪拼图。
                self.crop_only(run, cancel)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.set_session('failed', reason=f"{type(e).__name__}: {e}")
            self.event(run, "failed", error=f"{type(e).__name__}: {e}",
                       traceback=traceback.format_exc()[-900:])
        finally:
            if self.session_state in ('running', 'stopping', 'starting'):
                self.set_session('completed')
            if self.local_preview:
                self.preview_stop.set()
                if self.preview_thread:
                    self.preview_thread.join(timeout=6)
                    self.preview_thread = None
                with self.preview_lock:
                    self.preview_cache.clear()
                while not self.preview_results.empty():
                    self.preview_results.get_nowait()
                self.event(run, 'local_preview_stats', **self.preview_stats)
            try:
                self._close_results()
            except Exception as e:
                self.set_session('failed', reason='result_journal_close_failed')
                self.event(run, "failed", error=f"Result journal close failed: {type(e).__name__}: {e}")
            if self._result_stats is not None:
                self.event(run, "result_stats", producer_instance_id=self.producer_instance_id,
                           **self._result_stats)
            self.event(run, "role_finished")
            if self.active_run == run["run_id"]:
                self.active_run = None
            if self.dataset_archive:
                self.dataset_archive.finish_session(run['run_id'], self.session_state, self.session_reason)
            if self.local_preview:
                self.local_preview.set_run(None)

    def load_vision(self, run):
        from .vision import Vision
        begin = time.monotonic()
        if self.vision is None:
            self.vision = Vision(self.c, edge=self.role == "edge")
        self.vision.reset()
        if hasattr(self.vision,'configure_run'):
            self.vision.configure_run(run)
        self.event(run, "vision_ready", load_ms=(time.monotonic() - begin) * 1000,
                   config_hash=canonical_hash(self.c), backend="ncnn" if self.role == "edge" else "pytorch-cuda",
                   assets_manifest_sha256=hashlib.sha256((ROOT / "PROVENANCE.json").read_bytes()).hexdigest(),
                   pose_shape=[self.c["pose_imgsz"]] * 2, clothing_shape=[512, 512], rect=False,
                   person_selection={k: self.c[k] for k in
                                     ("max_people", "min_person_confidence", "min_person_height_ratio", "pose_max_det")})

    def model_manifest(self):
        """P2-10：模型清单只在首次会话前收集一次（权重哈希较慢，之后走缓存）。"""
        cached=getattr(self,'_model_manifest',None)
        if cached is None:
            from .model_manifest import collect
            try:
                cached=collect(self.c,ROOT,self.role,device='pi-cpu' if self.role=='edge' else 'cuda:0')
            except Exception as error:  # noqa: BLE001 - 清单失败不能挡住采集
                cached={'schema_version':'1.0','role':self.role,'device':None,'models':[],
                        'error':type(error).__name__+': '+str(error)}
            self._model_manifest=cached
        return cached

    def edge(self, run, cancel):
        import cv2
        from .vision import observation
        c, arm = {**self.c, 'sample_fps': run.get('sample_fps', self.c['sample_fps'])}, run["arm"]
        a_mode = run.get('arm_a_mode', c.get('a_mode', 'detect'))
        # getattr 兜底：单测会用 object.__new__ 构造 Worker，不经过 __init__。
        policy = getattr(self, 'policy', None)
        if policy is not None:
            policy.start_session(c['sample_fps'])
        gate = getattr(self, 'scene_gate', None)
        if gate is not None:
            gate.reset()
            gate.set_epoch(run['run_id'])
        health = getattr(self, 'health', None)
        health_state = None
        if health is not None:
            health.start_session()
            health_state = health.state
        if run["source"] == "camera" and not c["allow_camera"]:
            raise RuntimeError("Camera disabled: use replay or a separately assigned camera")
        if arm == "A" and a_mode == 'full':
            self.load_vision(run)
        if arm == "B" and self.gate is None:
            from ultralytics import YOLO
            gate_path = ROOT / "assets/yolo11n_ncnn_model"
            if not gate_path.exists():
                raise RuntimeError("Export the person gate to NCNN with tools/export_gate.py first")
            self.gate = YOLO(str(gate_path))
        replay = run["source"] == "replay"
        source = ROOT / c["replay_file"] if replay else c["camera_index"]
        if run.get('upload_id'):
            from .video_uploads import video_path
            source = video_path(run['upload_id'])
        source_hash = None
        if replay:
            digest = hashlib.sha256()
            with Path(source).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
            source_hash = digest.hexdigest()
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
                 "frame_bytes": 0, "observation_bytes": 0, "preview_bytes": 0,
                 "publish_failures": 0, "policy_dropped": 0,
                 "gate_seen": 0, "gate_would_skip": 0, "gate_skipped": 0,
                 "hard_examples_saved": 0, "hard_examples_duplicate": 0,
                 "hard_examples_rejected": 0}
        self.event(run, "source_open", source_sha256=source_hash, config_hash=canonical_hash(c),
                   source=run['source'], capture_backend='opencv' if replay else c.get('camera_backend', 'opencv'),
                   requested_capture_fps=source_fps,
                   requested_sample_fps=c['sample_fps'],
                   requested_size=[c['width'], c['height']] if not replay else None)
        def capture():
            i = 0
            try:
                while not cancel.is_set() and not self.stop.is_set() and (run.get("unlimited",False) or time.monotonic() - start < run["seconds"]):
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
        last_sample = last_preview = last_fallback = -1e9
        gate_blocked = False
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
                # 应用级就绪：没有结果就 refresh 到 OFFLINE；放在门控之前，
                # 保证"健康状态与本地显示"不被媒体准入逻辑短路（FIX-07）。
                if health is not None:
                    current_state = health.refresh()
                    if current_state != health_state:
                        self.event(run, 'health_state', state=current_state,
                                   previous=health_state, recovery_s=health.recovery_s)
                        health_state = current_state
                # 场景门控：只在有外部证据的"已复核为空"场景降频；
                # 只有 active 模式才真的拦帧；离线兜底还需要画面时不能提前 continue。
                gate_blocked = False
                gate_decision = None
                if gate is not None and arm != 'A':
                    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    # 人物存在性护栏：最近 person_guard_s 内云端报告过轨迹，就不允许
                    # 门控进入空场景分支。静坐的人帧差很小，没有这道护栏会误滤。
                    guard_s = float((self.c.get('collaboration') or {}).get('person_guard_s', 10))
                    guarded = (self.last_person_seen is not None
                               and time.monotonic() - self.last_person_seen < guard_s)
                    gate_decision = gate.decide(gray, task_active=(arm == 'B') or guarded,
                                                now=time.monotonic())
                    stats['gate_seen'] += 1
                    if not gate_decision['allowed']:
                        stats['gate_would_skip'] += 1
                        if policy is not None and policy.enforcing:
                            need_fallback = (self.local_preview
                                             and health_state in ('OFFLINE', 'WARMING'))
                            if not need_fallback:
                                stats['gate_skipped'] += 1
                                continue
                            gate_blocked = True
                # Same JPEG is decoded before A/B inference and transmitted by B/C.
                # 说明：只有 active 模式才按档位改画质；shadow 必须与原基线逐字节一致。
                quality = c["jpeg_quality"]
                if policy is not None and policy.enforcing:
                    tier = policy.quality(time.monotonic())
                    if tier is not None:
                        quality = tier
                flags = [cv2.IMWRITE_JPEG_QUALITY, quality]
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
                header.update(collection_batch_id=run.get('collection_batch_id',run['run_id']),
                              collection_location=run.get('collection_location','未声明'),
                              collection_name=run.get('collection_name',''),source_zone_id=run.get('source_zone_id','lab'))
                header["encoded_size"] = [image.shape[1], image.shape[0]]
                header["jpeg_quality"] = quality
                header['jpeg_optimize'] = c.get('jpeg_optimize', False)
                # W1：常态/漏检候选采样；被门控拦下的帧不采（它们本来就没被复核）。
                self._maybe_save_hard_example(run, header, jpeg, gate_decision,
                                              health_state, arm, stats)
                if arm == "B":
                    gate_start = time.perf_counter_ns()
                    r = self.gate.predict(decoded, imgsz=c["gate_imgsz"], classes=[0],
                                          conf=max(.001,run.get("confidence_min",.25)), device="cpu", rect=False, verbose=False)[0]
                    header["gate_ms"] = (time.perf_counter_ns() - gate_start) / 1e6
                    from .recognition_region import settings
                    low,high,region=settings(run)
                    if not any(k in run for k in ('confidence_min','confidence_max','recognition_region')):
                        qualifying=[True] if len(r.boxes) else []
                        boxes=[];scores=[]
                    else:
                        boxes=r.boxes.xyxy.cpu().tolist();scores=r.boxes.conf.cpu().tolist()
                        qualifying=[]
                    for box,score in zip(boxes,scores):
                        if not low<=score<=high:continue
                        if region['mode']=='rectangle':
                            (rx1,ry1),(rx2,ry2)=region['points'];hh,ww=decoded.shape[:2]
                            cx,cy=(box[0]+box[2])/(2*ww),(box[1]+box[3])/(2*hh)
                            if not (rx1<=cx<=rx2 and ry1<=cy<=ry2):continue
                        qualifying.append(box)
                    if not qualifying:
                        self.event(run, "gate_negative", source_frame_id=index, source_pts_s=pts,
                                   gate_ms=header["gate_ms"])
                        # 门控阴性但画面有运动 → 最有价值的漏检候选
                        self._maybe_save_hard_example(run, header, jpeg, gate_decision,
                                                      health_state, arm, stats,
                                                      gate_state='negative')
                        preview_slot = next_preview_slot(pts, c['preview_hz'], last_preview)
                        if preview_slot is not None:
                            gate_obs = observation(header, [], header['gate_ms'], 'edge-gate')
                            gate_obs['status'] = 'person_gate_negative_not_cloud_verified'
                            stats['preview_bytes'] += self.publish_preview(run, preview_packet(header, decoded, gate_obs, c))
                            last_preview = preview_slot
                        continue
                    stats["gate_positive"] += 1
                if arm == "A" and a_mode == 'full':
                    tracks, ms = self.vision.infer(decoded, pts)
                    if cancel.is_set():
                        break
                    out = observation(header, tracks, ms, "edge")
                    out.update(getattr(self.vision,"last_region_evidence",{}))
                    stats["observation_bytes"] += self.publish_observation(run, out)
                elif arm == "A":
                    if self.object_line is not None:
                        self.object_line.reset()
                    if self.object_line is None:
                        from .object_line import ObjectLine
                        self.object_line = ObjectLine(
                            c, lambda run_, meta, jpeg: self.publish_crops(run_, meta, jpeg), self.event,
                            lambda track_id: self.crop_attr(track_id))
                    tracks, ms = self.object_line.infer(decoded, pts, run, header)
                    if cancel.is_set():
                        break
                    out = observation(header, tracks, ms, "edge-object")
                    out['object_line'] = True
                    stats["observation_bytes"] += self.publish_observation(run, out)
                else:
                    packet = pack(header, jpeg)
                    send = not gate_blocked
                    if gate_blocked:
                        stats['gate_skipped'] += 1
                        stats['policy_dropped'] += 1
                    if policy is not None and send:
                        policy.observe_frame(len(jpeg), time.monotonic())
                        pending = [info for info in getattr(self.media(), 'abc_frame_pending', [])
                                   if not info.is_published()]
                        decision = policy.decide(
                            len(packet), queue_items=len(pending),
                            # queue_bytes 用"在途帧数 × 本帧字节"作为保守上界；
                            # 传输层不暴露逐包大小，这里不假装它是精确值（FIX-06）。
                            queue_bytes=len(pending) * len(packet),
                            age_s=max(0.0, (time.monotonic_ns() - ready) / 1e9),
                            gpu_ready=(health_state != 'OFFLINE') if health is not None else True)
                        # 影子模式只记账不改变行为；active 模式按判定执行。
                        send = decision['allowed'] or not decision['enforced']
                        if not send:
                            stats["policy_dropped"] += 1
                    if send:
                        self.cache_preview_source(header, jpeg)
                        sent = publish(self.media(), self.root + f'/runs/{run["run_id"]}/frames', packet)
                        stats["frame_bytes"] += sent
                        stats["publish_failures"] += not bool(sent)
                        if policy is not None:
                            if sent:
                                policy.record_sent(sent)
                            elif health_state == 'OFFLINE':
                                policy.record_drop('offline')
                            else:
                                policy.record_drop('queue_full')
                # 云端结果断了也要让本地画面继续：发"待分析"帧，不贴上一帧的框。
                if (self.local_preview and arm != 'A' and health is not None
                        and health_state in ('OFFLINE', 'WARMING')):
                    fallback_slot = next_preview_slot(pts, c['preview_hz'], last_fallback)
                    if fallback_slot is not None:
                        stub = {**header, 'kind': 'observation', 'producer': 'edge-fallback',
                                'status': 'analysis_offline', 'tracks': [], 'infer_ms': None,
                                'observation_seq': 0}
                        self.preview_stats['fallback_packets'] += 1
                        try:
                            self.publish_preview(run, preview_packet(header, image, stub, c))
                        except Exception as error:  # noqa: BLE001 - 预览失败不能中断会话
                            self.event(run, 'preview_failed', error=f'{type(error).__name__}: {error}')
                        last_fallback = fallback_slot
                preview_slot = next_preview_slot(pts, c['preview_hz'], last_preview)
                if arm == "A" and preview_slot is not None:
                    stats["preview_bytes"] += self.publish_preview(run, preview_packet(header, decoded, out, c))
                    last_preview = preview_slot
        finally:
            cancel.set()
            thread.join(timeout=3)
            duration = time.monotonic() - start
            import resource
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            payload = {'duration_s': duration,
                       'media_route': 'direct_gpu' if c.get('media_enabled') else 'control_cloud',
                       'threads': threading.active_count(), 'max_rss_mb': round(rss_mb, 1),
                       'rx_attrs': self.rx_attrs, 'crop_attrs_cached': len(self.crop_attrs),
                       'object_line': (None if getattr(self, 'object_line', None) is None else
                                       {'last_infer_ms': round(getattr(self.object_line, 'last_infer_ms', 0.0), 1),
                                        'skipped_slow': getattr(self.object_line, 'skipped_slow', 0)}),
                       **stats}
            if policy is not None:
                payload['policy'] = policy.summary(duration_s=duration)
            if gate is not None:
                payload['scene_gate'] = gate.stats
            if health is not None:
                payload['health'] = health.snapshot()
            store = getattr(self, 'sample_store', None)
            if store is not None:
                payload['hard_examples'] = store.stats()
            self.event(run, "transport_stats", **payload)
            self._record_collaboration(run, payload)

    def _record_collaboration(self, run, payload):
        """把本轮的策略/门控/健康摘要追加到本地 jsonl，供离线复盘。

        刻意只写摘要（每轮一行），不写逐帧决策：Pi 的 SD 卡和 MQTT 都不该为
        观测数据付钱。写失败绝不影响会话结束。
        """
        try:
            directory = ROOT / 'state' / 'collaboration'
            directory.mkdir(parents=True, exist_ok=True)
            policy = payload.get('policy') or {}
            record = {
                'ts': round(time.time(), 3),
                'run_id': run['run_id'], 'arm': run['arm'], 'source': run.get('source'),
                'duration_s': round(payload.get('duration_s') or 0, 3),
                'sampled': payload.get('sampled'), 'frame_bytes': payload.get('frame_bytes'),
                'mode': policy.get('mode'), 'budget_bps': policy.get('budget_bps'),
                'target_fps': policy.get('target_fps'),
                'sent_frames': policy.get('sent_frames'), 'sent_bps': policy.get('sent_bps'),
                'dropped': policy.get('dropped') or {},
                'scene_gate': {k: v for k, v in (payload.get('scene_gate') or {}).items()
                               if k in ('frames_seen', 'allowed', 'skipped', 'by_reason', 'by_state')},
                'health': {k: v for k, v in (payload.get('health') or {}).items()
                           if k in ('state', 'recovery_s', 'transitions')},
                'preview_fallback_packets': self.preview_stats.get('fallback_packets', 0),
            }
            name = time.strftime('%Y-%m-%d') + '.jsonl'
            with (directory / name).open('a') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        except (OSError, TypeError, ValueError):
            pass

    def _maybe_save_hard_example(self, run, header, jpeg, gate_decision,
                                 health_state, arm, stats, gate_state=None):
        """W1：按触发理由把这一帧写进有界暂存箱；失败绝不影响会话。"""
        selector = getattr(self, 'hard_selector', None)
        store = getattr(self, 'sample_store', None)
        if selector is None or store is None:
            return None
        # 应用未就绪时的帧没有"模型判断"可复核，采了只会污染数据集（复核文档 §4.2）。
        if health_state in ('WARMING', 'OFFLINE'):
            return None
        try:
            changed = (gate_decision or {}).get('changed_ratio')
            decision = selector.consider(changed_ratio=changed, gate_state=gate_state, arm=arm)
            if not decision['capture']:
                return None
            meta = {
                'task': 'person_detection',
                'run_id': run['run_id'],
                'policy_epoch': run['run_id'],
                'collection_batch_id':run.get('collection_batch_id',run['run_id']),
                'collection_location':run.get('collection_location','未声明'),
                'collection_name':run.get('collection_name',''),
                'source_frame_ids': [header.get('source_frame_id')],
                'source_pts_range_s': [header.get('source_pts_s'), header.get('source_pts_s')],
                'student_model_version': None,
                'capture_config_hash': canonical_hash({
                    k: self.c.get(k) for k in
                    ('width', 'height', 'jpeg_quality', 'encode_max_side', 'sample_fps')}),
                'trigger_reasons': decision['reasons'],
                'sampling_probability': decision['sampling_probability'],
                'arm': arm,
                'source': run.get('source'),
                'jpeg_quality': header.get('jpeg_quality'),
                'encoded_size': header.get('encoded_size'),
                'gate_state': gate_state or (gate_decision or {}).get('state'),
                'changed_ratio': None if changed is None else round(float(changed), 5),
                'health_state': health_state,
                'policy_mode': getattr(getattr(self, 'policy', None), 'mode', None),
            }
            result = store.put(jpeg, meta)
            if result['status'] == 'saved':
                stats['hard_examples_saved'] += 1
            elif result['status'] == 'duplicate':
                stats['hard_examples_duplicate'] += 1
            else:
                stats['hard_examples_rejected'] += 1
            return result
        except Exception:  # noqa: BLE001 - 采集失败不能中断会话
            stats['hard_examples_rejected'] += 1
            return None

    def publish_crops(self, run, meta, jpeg):
        client = self.media()
        if client is None:
            return 0
        try:
            packet = pack(meta, jpeg)
            publish(client, self.root + f'/runs/{run["run_id"]}/crops', packet, qos=0)
            return len(packet)
        except Exception:  # noqa: BLE001 - 裁剪上传失败不能中断采集
            return 0

    def crop_attr(self, track_id, ttl_s=2.5):
        with self.crop_attrs_lock:
            item = self.crop_attrs.get(track_id)
            if not item:
                return None
            if time.monotonic() - item.get('at', 0) > ttl_s:
                self.crop_attrs.pop(track_id, None)
                return None
            return item

    def _publish_crops_result(self, run, meta, tracks, infer_ms):
        now = time.monotonic()
        attrs = []
        with self.crop_attrs_lock:
            for item in tracks:
                attrs.append({"track_id": item.get("track_id"),
                              "class_name": (item.get("attributes") or {}).get("class_name"),
                              "posture": (item.get("attributes") or {}).get("posture"),
                              "clothing": (item.get("attributes") or {}).get("clothing") or [],
                              "crop_confidence": (item.get("attributes") or {}).get("crop_confidence")})
                if item.get("track_id") is not None:
                    self.crop_attrs[item["track_id"]] = {**attrs[-1], 'at': now}
        publish(self.client, self.root + f'/runs/{run["run_id"]}/crop_attrs',
                {"project": self.c["project"], "run_id": run["run_id"], "arm": "A",
                 "kind": "crop_attrs", "source_frame_id": meta.get("source_frame_id"),
                 "infer_ms": infer_ms, "attrs": attrs}, qos=0)

    def crop_only(self, run, cancel):
        deadline = float('inf') if run.get('unlimited', False) else time.monotonic() + run['seconds'] + 15
        thread = threading.Thread(target=self.crop_loop, args=(run, cancel, deadline), daemon=True)
        thread.start()
        while not cancel.is_set() and not self.stop.is_set() and time.monotonic() < deadline:
            time.sleep(.2)
        cancel.set()
        thread.join(timeout=6)

    def crop_loop(self, run, cancel, deadline=None):
        from .crop_infer import CropInfer
        if self.crop_infer is None:
            self.crop_infer = CropInfer(self.c)
        handled = 0
        debug = {'dequeued': 0, 'slot_total': 0, 'bytes': 0, 'tiles_decoded': 0, 'errors': 0}
        while (not cancel.is_set() and not self.stop.is_set()
               and (deadline is None or time.monotonic() < deadline)):
            try:
                header, jpeg = self.crops.get(timeout=.2)
            except queue.Empty:
                continue
            debug['dequeued'] += 1
            debug['bytes'] += len(jpeg)
            debug['slot_total'] += len(header.get('slots') or [])
            if header.get('run_id') != run['run_id']:
                continue
            try:
                count = self.crop_infer.handle(
                    header, jpeg,
                    lambda meta, tracks, infer_ms: self._publish_crops_result(run, meta, tracks, infer_ms))
                handled += count
                debug['tiles_decoded'] += 1 if count else 0
            except Exception as error:  # noqa: BLE001 - 单批失败只记事件
                debug['errors'] += 1
                self.event(run, 'crop_infer_failed', error=type(error).__name__ + ': ' + str(error))
        self.event(run, 'crop_stats', handled=handled, received=self.rx_crops, **debug)

    def cloud(self, run, cancel):
        import cv2
        import numpy as np
        from .vision import observation
        self.load_vision(run)
        crop_thread = threading.Thread(target=self.crop_loop, args=(run, cancel), daemon=True)
        crop_thread.start()
        received_start, overwritten_start = self.rx_frames, self.frame_overwrites
        processed = 0
        deadline = float("inf") if run.get("unlimited",False) else time.monotonic() + run["seconds"] + 10
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
            out.update(getattr(self.vision,"last_region_evidence",{}))
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
