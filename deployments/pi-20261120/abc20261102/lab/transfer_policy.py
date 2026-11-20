"""传输预算与准入策略（纯标准库；影子模式不改变任何线上行为）。

设计依据：
- `02_技术方案与讨论/边云协作架构优化方案_20261116.md` §5.1（字节令牌桶）、§5.3（快/慢两层）
- 验收复核 FIX-02：**硬预算不得被动作底线绕过**

单位约定：预算一律用 **bit/s** 表达，内部令牌桶按 **byte/s** 计费。

准入判定（先安全、后省字节）：

1. `offline`        GPU 应用未就绪 → 只放行"探针帧"（有限配额），其余弃帧。
2. `expired`        帧龄超过 deadline → 弃帧。
3. `queue_full`     在途队列已满 → 弃帧（任何模式都不放松）。
4. `queue_bytes`    在途字节超上限 → 弃帧。
5. `pacing`         本轮节拍不允许 → 弃帧。
6. `byte_budget`    令牌不足 → 弃帧。
7. `ok`             放行并扣费。

关键不变量（FIX-02 的验收口径）：
**任意滑动区间内放行的媒体字节 ≤ rate × 时长 + burst**。动作底线不是"绕过预算的许可证"：
当预算无法支撑 8 Hz 时，目标帧率降到可负担值，并把本轮标记为 `insufficient_sampling`
（显式声明动作证据不足），而不是偷偷超发。

`mode`：`off` / `shadow`（只计算与记账，调用方照常发送）/ `active`（按判定执行）。
"""

from collections import Counter, deque
import time

REASONS = ('ok', 'offline_probe', 'byte_budget', 'pacing',
           'queue_full', 'queue_bytes', 'expired', 'offline')

DEFAULTS = {
    'enabled': False,
    'mode': 'shadow',                # off | shadow | active
    'media_budget_bps': 0,           # 0 = 未标定，禁止接管
    'alpha': 0.8,                    # 留余量系数
    'burst_s': 0.25,                 # 令牌桶允许的突发时长
    # 在途上限：传输层已用 media_ack_window 自带上界，这里默认放宽，
    # 避免策略与 PUBACK 窗口重复丢弃、把抖动放大成吞吐损失。
    'max_queue_items': 64,
    'max_queue_bytes': 2000000,
    'deadline_ms': 500.0,
    'action_floor_fps': 8.0,         # TCN 8 Hz 时间网格的工程底线（只用于判定是否声明证据不足）
    'action_floor_window_s': 2.0,
    'offline_probe_interval_s': 3.0, # 离线时每隔这么久放一帧探针，用于确认恢复（FIX-01）
    'pacing': True,
    'target_fps_round': 0.5,
    'ema_alpha': 0.1,
    'quality_tiers': [],             # 由高到低；空列表 = 不改画质
    'tier_up_ratio': 0.6,
    'tier_down_ratio': 0.92,
    'tier_dwell_s': 5.0,
}


class TokenBucket:
    """按字节计费的令牌桶；容量 = rate * burst_s，不允许透支。"""

    def __init__(self, rate_bytes_s, burst_s=0.25, clock=time.monotonic):
        self.rate = max(0.0, float(rate_bytes_s))
        self.capacity = max(0.0, self.rate * float(burst_s))
        self.tokens = self.capacity
        self.clock = clock
        self._last = clock()

    def refill(self, now):
        elapsed = now - self._last
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last = now

    def take(self, nbytes, now=None):
        """够则扣费返回 True；不够返回 False 且不扣。单帧超容量按拒绝处理。"""
        now = self.clock() if now is None else now
        self.refill(now)
        if nbytes > self.capacity > 0:
            return False
        if self.tokens >= nbytes:
            self.tokens -= nbytes
            return True
        return False

    def reset(self):
        self.tokens = self.capacity
        self._last = self.clock()


class TierSelector:
    """画质档位：利用率低就升档，持续打满就降档；带最短驻留时间防抖。"""

    def __init__(self, tiers, up_ratio=0.6, down_ratio=0.92, dwell_s=5.0, clock=time.monotonic):
        self.tiers = [int(t) for t in (tiers or [])]
        self.up_ratio = float(up_ratio)
        self.down_ratio = float(down_ratio)
        self.dwell_s = float(dwell_s)
        self.clock = clock
        self.index = 0
        self.changes = 0
        self._since = clock()

    def current(self):
        return self.tiers[self.index] if self.tiers else None

    def update(self, utilization, now=None):
        now = self.clock() if now is None else now
        if not self.tiers or now - self._since < self.dwell_s:
            return self.current()
        moved = False
        if utilization >= self.down_ratio and self.index < len(self.tiers) - 1:
            self.index += 1
            moved = True
        elif utilization <= self.up_ratio and self.index > 0:
            self.index -= 1
            moved = True
        if moved:
            self.changes += 1
            self._since = now
        return self.current()

    def reset(self):
        self.index = 0
        self.changes = 0
        self._since = self.clock()


class MediaBudgetPolicy:
    """把"预算 + deadline + 队列 + 节拍 + 画质档位"合成一个逐帧准入判定。"""

    def __init__(self, config=None, clock=time.monotonic):
        self.c = {**DEFAULTS, **(config or {})}
        self.clock = clock
        self.mode = 'off' if not self.c['enabled'] else str(self.c['mode'])
        self.budget_bps = float(self.c['media_budget_bps'] or 0)
        aligned = self.budget_bps > 0 and self.mode in ('shadow', 'active')
        self.active_control = aligned
        effective_bps = self.budget_bps * float(self.c['alpha']) if aligned else 0.0
        self.bucket = TokenBucket(effective_bps / 8.0, self.c['burst_s'], clock)
        self.tiers = TierSelector(self.c['quality_tiers'], self.c['tier_up_ratio'],
                                  self.c['tier_down_ratio'], self.c['tier_dwell_s'], clock)
        self.process_totals = {'sessions': 0, 'sent_frames': 0, 'sent_bytes': 0, 'dropped': 0}
        self.sample_fps = float(self.c.get('sample_fps') or 12)
        self.ema_bytes = None
        self.target_fps = None
        self.insufficient_sampling = False
        self.offline_probes = 0
        self._target_at = 0.0
        self._pace_index = 0
        self._last_probe = None
        self._reset_session_counters()

    # -- 对外 -------------------------------------------------------------
    @property
    def enabled(self):
        return self.mode in ('shadow', 'active')

    @property
    def enforcing(self):
        return self.mode == 'active'

    def start_session(self, sample_fps, now=None):
        """每轮会话开始调用：**完整重置会话计数**，只把累计量累加到 process_totals。"""
        now = self.clock() if now is None else now
        self.process_totals['sessions'] += 1
        self.process_totals['sent_frames'] += self.sent_frames
        self.process_totals['sent_bytes'] += self.sent_bytes
        self.process_totals['dropped'] += sum(self.dropped.values())
        self._reset_session_counters()
        self.sample_fps = float(sample_fps or 0)
        self.ema_bytes = None
        self.target_fps = None
        self.insufficient_sampling = False
        self.offline_probes = 0
        self._target_at = 0.0
        self._pace_index = 0
        self._last_probe = None
        self._start = now
        self.bucket.reset()
        self.tiers.reset()

    def _reset_session_counters(self):
        self.would_frames = 0
        self.would_bytes = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        self.dropped = Counter()
        self.decisions = 0
        self._sends = deque()
        self._start = self.clock()

    def quality(self, now=None):
        """当前画质档位；无档位配置时返回 None（调用方用配置默认值）。"""
        now = self.clock() if now is None else now
        if self.enabled and self.tiers.tiers and self.active_control:
            self.tiers.update(self.utilization(now), now)
        return self.tiers.current()

    def observe_frame(self, nbytes, now=None):
        """每编码出一帧就调用（无论最终是否下发），用于估计这一档画质多大。"""
        if not nbytes or nbytes <= 0:
            return
        alpha = float(self.c['ema_alpha'])
        self.ema_bytes = float(nbytes) if self.ema_bytes is None else (
            (1.0 - alpha) * self.ema_bytes + alpha * float(nbytes))
        # 桶容量至少容纳一帧：预算很小时若容量 < 单帧，链路会永远发不出任何帧
        # （不是"严格"，是死锁）。这只是一帧的固定突发，仍在"≤ rate×t + burst"口径内。
        if self.bucket.rate > 0:
            self.bucket.capacity = max(self.bucket.rate * float(self.c['burst_s']),
                                       self.ema_bytes * 1.1)
            self.bucket.tokens = min(self.bucket.tokens, self.bucket.capacity)

    # -- 节拍与目标帧率 ----------------------------------------------------
    def _recompute_target(self, now):
        """目标帧率 = min(采样率, 预算 ÷ 观测到的单帧字节)。

        注意：**不把目标帧率抬到动作底线**。预算撑不住 8 Hz 时，目标帧率就是可负担值，
        同时置 `insufficient_sampling`，由调用方把它写进结果/遥测——这是显式声明，
        不是绕过预算的理由（FIX-02）。
        """
        if not self.active_control or not self.ema_bytes or self.sample_fps <= 0:
            self.target_fps = self.sample_fps or None
            return self.target_fps
        affordable = self.bucket.rate / self.ema_bytes          # byte/s ÷ byte/frame = fps
        target = min(self.sample_fps, affordable)
        step = max(1e-3, float(self.c['target_fps_round'] or 0.5))
        target = max(step, int(target / step + 1e-9) * step)
        self.target_fps = min(target, self.sample_fps)
        floor = float(self.c['action_floor_fps'] or 0)
        self.insufficient_sampling = bool(floor > 0 and self.target_fps < floor)
        self._target_at = now
        return self.target_fps

    def target_fps_value(self, now=None):
        now = self.clock() if now is None else now
        if self.target_fps is None or now - self._target_at > 1.0:
            self._recompute_target(now)
        return self.target_fps

    def _paced(self, now):
        """按帧序号的均匀抽帧（Bresenham），与墙钟无关。

        Pi 实际帧间隔是 33/67 ms 交替（30 fps 采集 + 20 Hz 源时间槽）；任何基于
        "距上次发送时间"的判据都会在这种网格上系统性丢一半节拍（实测 14.5 fps → 10 fps）。
        """
        if not self.c['pacing'] or not self.active_control:
            return False
        target = self.target_fps_value(now)
        if not target or not self.sample_fps or self.sample_fps <= 0:
            return False
        self._pace_index += 1
        ratio = min(1.0, float(target) / float(self.sample_fps))
        allow = int((self._pace_index + 1) * ratio + 1e-9) > int(self._pace_index * ratio + 1e-9)
        return not allow

    def _probe_due(self, now):
        """离线探针：每 offline_probe_interval_s 允许一帧，用来确认恢复（FIX-01）。"""
        interval = float(self.c['offline_probe_interval_s'] or 0)
        if interval <= 0:
            return False
        if self._last_probe is None or now - self._last_probe >= interval:
            self._last_probe = now
            return True
        return False

    # -- 判定 -------------------------------------------------------------
    def decide(self, nbytes, now=None, queue_items=0, queue_bytes=0,
               age_s=0.0, gpu_ready=True, task_active=False):
        """返回判定 dict；`enforced=False` 时调用方必须照常发送（影子模式）。

        `task_active` 只为将来做优先级预留：**它不解除预算**（FIX-02）。
        """
        now = self.clock() if now is None else now
        self.decisions += 1
        if not self.enabled or not self.active_control:
            # 未启用、或预算未标定：不做任何逐帧判定，避免"关掉策略反而引入新的丢弃路径"。
            return {'allowed': True, 'reason': 'ok', 'enforced': False,
                    'tokens': None, 'achieved_fps': None, 'quality': None, 'mode': self.mode}
        achieved = self.achieved_fps(now)
        if not gpu_ready:
            if self._probe_due(now):
                self.bucket.take(nbytes, now)
                self.offline_probes += 1
                allowed, reason = True, 'offline_probe'
            else:
                allowed, reason = False, 'offline'
        elif age_s * 1000.0 > float(self.c['deadline_ms']):
            allowed, reason = False, 'expired'
        elif queue_items >= int(self.c['max_queue_items']):
            allowed, reason = False, 'queue_full'
        elif queue_bytes + nbytes > int(self.c['max_queue_bytes']):
            allowed, reason = False, 'queue_bytes'
        elif self._paced(now):
            allowed, reason = False, 'pacing'
        elif self.bucket.take(nbytes, now):
            allowed, reason = True, 'ok'
        else:
            allowed, reason = False, 'byte_budget'
        if allowed:
            self.would_frames += 1
            self.would_bytes += nbytes
        else:
            self.dropped[reason] += 1
        return {'allowed': allowed, 'reason': reason, 'enforced': self.enforcing,
                'tokens': round(self.bucket.tokens, 1), 'achieved_fps': achieved,
                'quality': self.tiers.current(), 'mode': self.mode,
                'target_fps': self.target_fps,
                'insufficient_sampling': self.insufficient_sampling}

    def record_sent(self, nbytes, now=None):
        now = self.clock() if now is None else now
        self.sent_frames += 1
        self.sent_bytes += nbytes
        self._sends.append(now)
        cutoff = now - float(self.c['action_floor_window_s'])
        while self._sends and self._sends[0] < cutoff:
            self._sends.popleft()

    def record_drop(self, reason):
        self.dropped[reason] += 1

    def achieved_fps(self, now=None):
        now = self.clock() if now is None else now
        cutoff = now - float(self.c['action_floor_window_s'])
        while self._sends and self._sends[0] < cutoff:
            self._sends.popleft()
        if not self._sends:
            return None
        window = max(1e-6, now - self._sends[0])
        return len(self._sends) / window

    def utilization(self, now=None):
        if self.budget_bps <= 0:
            return 0.0
        elapsed = max(1e-6, (self.clock() if now is None else now) - self._start)
        return (self.would_bytes * 8.0 / elapsed) / self.budget_bps

    def summary(self, duration_s=None):
        now = self.clock()
        elapsed = duration_s if duration_s else max(1e-6, now - self._start)
        fps = self.achieved_fps(now)
        return {
            'enabled': self.enabled, 'mode': self.mode,
            'budget_bps': self.budget_bps,
            'effective_bps': round(self.bucket.rate * 8.0, 1),
            'decisions': self.decisions,
            'would_frames': self.would_frames, 'would_bytes': self.would_bytes,
            'would_bps': round(self.would_bytes * 8.0 / max(1e-6, elapsed), 1),
            'sent_frames': self.sent_frames, 'sent_bytes': self.sent_bytes,
            'sent_bps': round(self.sent_bytes * 8.0 / max(1e-6, elapsed), 1),
            'target_fps': self.target_fps,
            'sample_fps': self.sample_fps,
            'insufficient_sampling': self.insufficient_sampling,
            'ema_frame_bytes': None if self.ema_bytes is None else round(self.ema_bytes, 1),
            'achieved_fps': None if fps is None else round(fps, 2),
            'dropped': dict(self.dropped), 'dropped_total': sum(self.dropped.values()),
            'offline_probes': self.offline_probes,
            'quality': self.tiers.current(), 'quality_changes': self.tiers.changes,
            'process_totals': {**self.process_totals,
                               'dropped': self.process_totals['dropped'] + sum(self.dropped.values())},
        }
