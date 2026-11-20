"""GPU 应用级就绪状态机：OFFLINE / WARMING / READY / DEGRADED。

为什么不用 TCP 或进程状态判断：TCP 通只说明端口开着，进程 RUNNING 只说明 supervisor
把它拉起来了；真正要回答的是"这一路推理现在能不能给出对应本帧的结果"。
所以以**应用层结果**为唯一证据源：

- 会话开始时先 WARMING（模型加载、队列预热），拿到第一条 observation 才算 READY；
- 超过 `offline_after_s` 没有任何结果 → OFFLINE（不是"还在忙"，是"不可用"）；
- WARMING 超过 `warmup_timeout_s` 仍无结果 → 也判 OFFLINE（加载失败/订阅没就绪）；
- 连续 `degraded_streak` 条结果的队列或推理耗时超过阈值 → DEGRADED（还能出结果，但别指望实时）。

设计约束：
- 状态只由"本会话内见过什么"决定，不跨会话延续；
- 恢复时间 `recovery_s` 从进入 OFFLINE 的时刻量起，同一台机单调时钟，跨主机时间不相减；
- 纯标准库、无 I/O、时钟可注入，便于单测。
"""
import time

STATES = ('OFFLINE', 'WARMING', 'READY', 'DEGRADED')

DEFAULTS = {
    'offline_after_s': 6.0,      # 连续这么多秒没有任何结果 → 判离线
    'warmup_timeout_s': 25.0,    # 会话开始后这么久还一条结果都没有 → 判离线
    'degraded_queue_ms': 150.0,  # 单条结果的云端排队耗时阈值
    'degraded_infer_ms': 120.0,  # 单条结果的推理耗时阈值
    'degraded_streak': 3,        # 连续多少条超阈值才算降级
}


class GpuHealth:
    def __init__(self, config=None, clock=time.monotonic):
        self.c = {**DEFAULTS, **(config or {})}
        self.clock = clock
        self.state = 'OFFLINE'
        self.last_seen = None
        self.recovery_s = None
        self.since = clock()
        self.transitions = []
        self._warm_since = None
        self._streak = 0

    # -- 对外 -------------------------------------------------------------
    def start_session(self, now=None):
        now = self.clock() if now is None else now
        self.last_seen = None
        self.recovery_s = None
        self._streak = 0
        self._warm_since = now
        self._set('WARMING', now, 'session_start')

    def note_observation(self, now=None, queue_ms=None, infer_ms=None, status=None):
        """每收到一条本会话的云端结果调用一次。返回当前状态。"""
        now = self.clock() if now is None else now
        if self.state in ('OFFLINE', 'WARMING'):
            # 断线/冷启动后的第一条结果：这才是"恢复"的证据。
            offline_at = self.since if self.state == 'OFFLINE' else None
            self._set('READY', now, 'first_observation' if offline_at is None else 'recovered')
            if offline_at is not None:
                self.recovery_s = round(now - offline_at, 3)
        self.last_seen = now
        hot = ((queue_ms is not None and queue_ms > float(self.c['degraded_queue_ms']))
               or (infer_ms is not None and infer_ms > float(self.c['degraded_infer_ms'])))
        if hot:
            self._streak += 1
            if self._streak >= int(self.c['degraded_streak']) and self.state == 'READY':
                self._set('DEGRADED', now, 'slow_results')
        else:
            self._streak = 0
            if self.state == 'DEGRADED':
                self._set('READY', now, 'recovered_speed')
        return self.state

    def refresh(self, now=None):
        """在没收到结果时也要调用，负责把 WARMING/READY 推进到 OFFLINE。"""
        now = self.clock() if now is None else now
        if self.state == 'WARMING' and self.last_seen is None:
            if now - (self._warm_since or now) > float(self.c['warmup_timeout_s']):
                self._set('OFFLINE', now, 'warmup_timeout')
        elif self.last_seen is not None:
            if now - self.last_seen > float(self.c['offline_after_s']) and self.state != 'OFFLINE':
                self._set('OFFLINE', now, 'observation_gap')
        return self.state

    @property
    def ready(self):
        return self.state in ('READY', 'DEGRADED')

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        return {
            'state': self.state,
            'ready': self.ready,
            'last_seen_s': None if self.last_seen is None else round(now - self.last_seen, 3),
            'recovery_s': self.recovery_s,
            'since_s': round(now - self.since, 3),
            'transitions': [{'from': a, 'to': b, 'reason': r, 'at': round(t - self._t0, 3)}
                            for a, b, r, t in self.transitions],
        }

    # -- 内部 -------------------------------------------------------------
    _t0 = 0.0

    def _set(self, state, now, reason):
        if state == self.state:
            return
        if not self.transitions:
            self._t0 = now
        self.transitions.append((self.state, state, reason, now))
        self.state = state
        self.since = now
