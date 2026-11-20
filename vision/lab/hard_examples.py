"""难例触发选择器（W1）：决定"这一帧值不值得存下来"，纯逻辑、无 I/O。

第一轮只做**能在 Pi 上判定的**触发，不做"低置信度"这类需要模型分数的事：

- `random_audit`          固定周期的分层随机审计（覆盖高置信样本，避免只看疑难导致采样偏差）；
- `motion_without_person` 本地明显有运动，但最近一次云端结果是 0 条轨迹 → 漏检候选；
- `gate_negative_but_motion`  B 模式人物门控判阴性，但画面有明显运动 → 门控漏检候选。

刻意**不**把"低置信度"当无条件 trigger：Pi 在 C 模式下拿不到任务置信度；而把
"网络没收到结果"当难例会把运维问题混进训练集（复核文档 §4.2 明确禁止）。

每个触发都带 `sampling_probability` 与 `reason`，落到样本元数据里；这样以后才能分析
"选择性采样"带来的偏差，而不是把难例集当独立测试集。
"""
import time

REASONS = ('random_audit', 'motion_without_person', 'gate_negative_but_motion')

DEFAULTS = {
    'enabled': False,
    'random_audit_interval_s': 30.0,   # 每 30 s 采一张"常态"样本
    'min_gap_s': 5.0,                  # 任意两次采集之间的最小间隔（防同一个人刷屏）
    'motion_ratio_threshold': 0.02,    # 与 scene_gate 的 active_motion_ratio 对齐
    'motion_window_s': 3.0,            # "最近一次云端结果"的时效窗口
    'sampling_probability': 0.02,      # 随机审计的抽样概率（写进元数据，供偏差分析）
}


class HardExampleSelector:
    def __init__(self, config=None, clock=time.monotonic):
        self.c = {**DEFAULTS, **(config or {})}
        self.clock = clock
        self._last_capture = None
        self._last_random = None
        self._last_by_reason = {}
        self.last_observation = None      # {'t':..., 'tracks': int}
        self.reasons_fired = {}

    def note_observation(self, tracks, now=None):
        """记录最近一次云端结果（只需轨迹数量；不保存内容）。"""
        now = self.clock() if now is None else now
        self.last_observation = {'t': now, 'tracks': int(tracks or 0)}

    def consider(self, now=None, changed_ratio=None, gate_state=None, arm=None):
        """返回 {'capture': bool, 'reasons': [...], 'sampling_probability': float|None}。

        只读判定，不写文件；同一个 `now` 多次调用不会重复触发（幂等）。
        """
        now = self.clock() if now is None else now
        if not self.c['enabled']:
            return {'capture': False, 'reasons': [], 'sampling_probability': None}
        if self._last_capture is not None and now - self._last_capture < float(self.c['min_gap_s']):
            return {'capture': False, 'reasons': [], 'sampling_probability': None}

        reasons = []
        interval = float(self.c['random_audit_interval_s'] or 0)
        if interval > 0 and (self._last_random is None or now - self._last_random >= interval):
            reasons.append('random_audit')

        threshold = float(self.c['motion_ratio_threshold'] or 0)
        motion = changed_ratio is not None and changed_ratio >= threshold
        if motion and self._recent_observation_tracks(now) == 0:
            reasons.append('motion_without_person')
        if arm == 'B' and gate_state == 'negative' and motion:
            reasons.append('gate_negative_but_motion')

        if not reasons:
            return {'capture': False, 'reasons': [], 'sampling_probability': None}

        self._last_capture = now
        if 'random_audit' in reasons:
            self._last_random = now
        for reason in reasons:
            self._last_by_reason[reason] = now
            self.reasons_fired[reason] = self.reasons_fired.get(reason, 0) + 1
        return {'capture': True, 'reasons': reasons,
                'sampling_probability': float(self.c['sampling_probability'] or 0)}

    def _recent_observation_tracks(self, now):
        """最近一次云端结果是否新鲜；过期就当"不知道"，不触发漏检候选。"""
        obs = self.last_observation
        if not obs:
            return None
        if now - obs['t'] > float(self.c['motion_window_s']):
            return None
        return obs['tracks']

    def reset(self):
        self._last_capture = None
        self._last_random = None
        self._last_by_reason = {}
        self.last_observation = None
        self.reasons_fired = {}
