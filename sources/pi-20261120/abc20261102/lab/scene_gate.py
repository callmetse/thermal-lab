"""Differential empty-scene gate for the Pi -> GPU uplink.

The Pi 5 captures 640x480 @ 30 fps with Picamera2, keeps ``sample_fps``
frames per second (currently 20), JPEG-encodes them and ships them over
MQTT so that the GPU side can run pose / clothing / action (TCN)
inference.  This module answers one question per captured frame: is this
frame worth spending uplink bytes on?  It only says "no" for a scene that
has been *verified* empty, and it never says "no" while a task is running.

Contract
--------
* Pure Python, numpy only.   No I/O, no threads, no module-level mutable
  state: every bit of state lives on the instance.
* Deterministic: the only clock used is the injected ``clock`` callable,
  so tests can drive it with a fake clock and get identical decisions.
* Input is a single-channel grayscale image (``uint8``, ``H x W``, any
  size); it is block-averaged down to roughly 160x120 before comparison.
* Frames are compared to their predecessor, so what the gate measures is
  *frame-to-frame change*, not "presence".

State machine
-------------
``uncertain``
    No verified baseline yet.  Every frame is allowed; a streak of
    ``confirm_quiet_frames`` quiet frames promotes to ``empty_verified``.
``active``
    Motion or an active task was seen.  Every frame is allowed.  A quiet
    streak may promote straight back to ``empty_verified``.
``empty_verified``
    The scene was audited empty.  Frames are skipped except for a
    periodic audit (``audit_interval_s``) and the ``min_gap_s`` floor.

Known limitations (read before wiring this into the pipeline)
-------------------------------------------------------------
1. **Small or static targets can be filtered indefinitely.**  The gate
   only sees frame-to-frame difference: a person who has been standing
   still for longer than the audit period, a small object, or a target
   the JPEG/downscale pipeline washes out can look exactly like an empty
   room and be skipped for as long as they stay still.  Therefore
   ``empty_verified`` must never be trusted on its own: the GPU-side
   *auditor* has to re-check every audited frame and must be able to set
   ``task_active=True`` (or call :meth:`SceneGate.reset`) the moment it
   has reason to believe the room is occupied.
2. **Only for downsampling an audited-empty scene, never for skipping
   frames while people are present.**  The gate is a byte-saving filter
   for a room that has already been verified empty, not an occupancy
   detector.  If presence is known or suspected, pass
   ``task_active=True`` and keep the original sampling rate.
3. **The action (TCN) time window must not be touched.**  The TCN model
   consumes 16 time slots on an 8 Hz grid and needs a window span of at
   least 1.75 s with slot coverage >= 0.6.  Dropping even a few frames in
   the middle of an action window corrupts that window, so an active
   action task forces ``task_active=True``, which allows every frame and
   resets the quiet bookkeeping (decision priority 2).
4. **Global illumination changes** (lights switched, auto-exposure
   hunting, camera nudged) are not filtered out; they surface as motion
   and keep the gate in ``active`` until the scene settles again.
5. The gate does not suppress *all* traffic in ``empty_verified``: an
   audit frame still goes out every ``audit_interval_s``, and that audit
   stream is what makes the state recoverable.
"""

from __future__ import annotations

import time

import numpy as np

__all__ = ["SceneGate", "REASONS", "STATES"]

# Decision reasons -----------------------------------------------------------
REASON_FIRST_FRAME = "first_frame"
REASON_TASK_GUARD = "task_guard"
REASON_UNCERTAIN_OBSERVE = "uncertain_observe"
REASON_ACTIVE_OBSERVE = "active_observe"
REASON_AUDIT_DUE = "audit_due"
REASON_EMPTY_SCENE_SKIP = "empty_scene_skip"
REASON_MIN_GAP = "min_gap"

REASONS = frozenset(
    {
        REASON_FIRST_FRAME,
        REASON_TASK_GUARD,
        REASON_UNCERTAIN_OBSERVE,
        REASON_ACTIVE_OBSERVE,
        REASON_AUDIT_DUE,
        REASON_EMPTY_SCENE_SKIP,
        REASON_MIN_GAP,
    }
)

# Gate states ----------------------------------------------------------------
STATE_UNCERTAIN = "uncertain"
STATE_EMPTY_VERIFIED = "empty_verified"
STATE_ACTIVE = "active"

STATES = frozenset({STATE_UNCERTAIN, STATE_EMPTY_VERIFIED, STATE_ACTIVE})

# Clock comparisons are float business: 20 fps means 0.05 s gaps, which are
# not exactly representable, so every threshold test carries this tolerance.
_EPS = 1e-9

# Config defaults ------------------------------------------------------------
_DEFAULT_DOWNSCALE = 4
_DEFAULT_DIFF_THRESHOLD = 8.0
_DEFAULT_ACTIVE_MOTION_RATIO = 0.02
_DEFAULT_QUIET_RATIO = 0.005
_DEFAULT_CONFIRM_QUIET_FRAMES = 12
_DEFAULT_AUDIT_INTERVAL_S = 2.0
_DEFAULT_MIN_GAP_S = 0.05


def _number(config, key, default):
    """``config.get(key, default)`` coerced to float, falling back safely."""
    try:
        value = config.get(key, default)
    except AttributeError:  # not a mapping at all
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value, low, high):
    return low if value < low else high if value > high else value


class SceneGate:
    """Decide, per frame, whether an uplink detection frame may be skipped.

    Parameters
    ----------
    config:
        Optional mapping of tuning knobs; missing keys fall back to the
        defaults documented in :meth:`config`.
    sample_fps:
        Frame rate at which :meth:`decide` is called with new frames.  When
        positive it sets ``min_gap = 1 / sample_fps`` (the minimum spacing
        between two allowed frames); otherwise ``config['min_gap_s']`` is
        used.
    clock:
        Zero-argument callable returning seconds; injected so the module is
        deterministic and never reads wall time itself.
    """

    def __init__(self, config=None, sample_fps=20.0, clock=time.monotonic):
        cfg = config if config is not None else {}

        self._downscale_factor = max(1, int(_number(cfg, "downscale", _DEFAULT_DOWNSCALE)))
        self._diff_threshold = max(0.0, _number(cfg, "diff_threshold", _DEFAULT_DIFF_THRESHOLD))
        self._active_motion_ratio = _clamp(
            _number(cfg, "active_motion_ratio", _DEFAULT_ACTIVE_MOTION_RATIO), 0.0, 1.0
        )
        self._quiet_ratio = _clamp(_number(cfg, "quiet_ratio", _DEFAULT_QUIET_RATIO), 0.0, 1.0)
        self._confirm_quiet_frames = max(
            1, int(_number(cfg, "confirm_quiet_frames", _DEFAULT_CONFIRM_QUIET_FRAMES))
        )
        self._audit_interval_s = max(
            0.0, _number(cfg, "audit_interval_s", _DEFAULT_AUDIT_INTERVAL_S)
        )
        # FIX-03：进入 empty_verified 必须依赖外部"空场景"证据（detector/audit），
        # 只凭安静帧计数不足以声称"已复核为空"。
        self._require_evidence = bool(cfg.get("require_evidence", True))
        self._evidence_ttl_s = max(0.0, _number(cfg, "evidence_ttl_s", 10.0))

        try:
            fps = float(sample_fps)
        except (TypeError, ValueError):
            fps = 0.0
        self._sample_fps = fps
        if fps > 0.0:
            # At the sampling rate two neighbouring frames cannot both be worth
            # sending; 1 / sample_fps is the natural floor.
            self._min_gap_s = 1.0 / fps
        else:
            self._min_gap_s = max(0.0, _number(cfg, "min_gap_s", _DEFAULT_MIN_GAP_S))

        self._clock = clock if callable(clock) else time.monotonic

        # Mutable instance state (the only state this module has).
        self._prev = None            # downscaled float reference frame
        self._state = STATE_UNCERTAIN
        self._quiet_count = 0
        self._last_allow_ts = None   # last allowed=True (audit anchor in empty_verified)
        self._last_audit_ts = None   # last deliberate audit pass
        self._frames_seen = 0
        self._allowed_count = 0
        self._skipped_count = 0
        self._by_reason = {}
        self._by_state = {}
        self._evidence_kind = "UNKNOWN"   # EMPTY / PRESENT / UNKNOWN
        self._evidence_ts = None
        self._evidence_epoch = None
        self._epoch = None

    # ------------------------------------------------------------------ evidence
    def note_evidence(self, kind, now=None, epoch=None):
        """外部证据入口（检测器 / 周期审计 / 人工标注）。

        ``kind``：``EMPTY`` / ``PRESENT`` / ``UNKNOWN``。只有 ``EMPTY`` 且在
        ``evidence_ttl_s`` 内、epoch 一致，才允许进入 ``empty_verified``；
        ``PRESENT`` 会立刻把状态打回 ``active``。没有证据时门控永远停在
        ``uncertain``（放行所有帧）——这是有意的保守默认（FIX-03）。
        """
        kind = str(kind).upper()
        if kind not in ("EMPTY", "PRESENT", "UNKNOWN"):
            raise ValueError("evidence kind must be EMPTY / PRESENT / UNKNOWN")
        ts = float(self._clock()) if now is None else float(now)
        self._evidence_kind = kind
        self._evidence_ts = ts
        self._evidence_epoch = epoch
        if kind == "PRESENT":
            self._state = STATE_ACTIVE
            self._quiet_count = 0
        elif kind == "UNKNOWN" and self._state == STATE_EMPTY_VERIFIED:
            self._state = STATE_UNCERTAIN
            self._quiet_count = 0
        return kind

    def set_epoch(self, epoch):
        """绑定会话/策略 epoch；epoch 变化后旧证据自动失效。"""
        if epoch != self._epoch:
            self._epoch = epoch
            self._evidence_kind = "UNKNOWN"
            self._evidence_ts = None
            self._evidence_epoch = None
            if self._state == STATE_EMPTY_VERIFIED:
                self._state = STATE_UNCERTAIN
                self._quiet_count = 0

    def _evidence_valid(self, ts):
        if not self._require_evidence:
            return True
        if self._evidence_kind != "EMPTY" or self._evidence_ts is None:
            return False
        if ts - self._evidence_ts > self._evidence_ttl_s + _EPS:
            return False
        if self._evidence_epoch is not None and self._evidence_epoch != self._epoch:
            return False
        return True

    # ------------------------------------------------------------------ query
    @property
    def config(self):
        """Resolved tuning knobs (a copy; mutating it does not affect the gate)."""
        return {
            "downscale": self._downscale_factor,
            "diff_threshold": self._diff_threshold,
            "active_motion_ratio": self._active_motion_ratio,
            "quiet_ratio": self._quiet_ratio,
            "confirm_quiet_frames": self._confirm_quiet_frames,
            "audit_interval_s": self._audit_interval_s,
            "min_gap_s": self._min_gap_s,
            "sample_fps": self._sample_fps,
        }

    @property
    def state(self):
        """Current state: ``uncertain`` / ``empty_verified`` / ``active``."""
        return self._state

    @property
    def stats(self):
        """Running counters; ``frames_seen == allowed + skipped``."""
        return {
            "frames_seen": self._frames_seen,
            "allowed": self._allowed_count,
            "skipped": self._skipped_count,
            "by_reason": dict(self._by_reason),
            "by_state": dict(self._by_state),
            "state": self._state,
            "quiet_streak": self._quiet_count,
        }

    # ------------------------------------------------------------------- time
    def _since_audit(self, ts):
        """Seconds since the last deliberate audit (unbounded if there was none)."""
        if self._last_audit_ts is None:
            return float("inf")
        return max(0.0, ts - self._last_audit_ts)

    # ------------------------------------------------------------ frame prep
    def _prepare(self, gray):
        """Coerce *gray* into a downscaled float32-ish 2-D array, or ``None``.

        Degenerate inputs (empty array, scalars, unparsable objects) return
        ``None`` and are treated as "no usable reference frame" instead of
        raising: a broken frame must never take the pipeline down.
        """
        try:
            arr = np.asarray(gray)
        except Exception:  # noqa: BLE001 - never let a bad frame raise
            return None

        if arr.size == 0 or arr.ndim == 0:
            return None

        try:
            if arr.dtype == object:
                arr = arr.astype(np.float64)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            elif arr.ndim > 2:
                # e.g. HxWxC: collapse trailing axes into a single channel.
                arr = arr.reshape(arr.shape[0], arr.shape[1], -1)
                arr = arr.astype(np.float64).mean(axis=2)

            if arr.dtype != np.uint8:
                if np.issubdtype(arr.dtype, np.floating):
                    arr = np.nan_to_num(arr, nan=0.0, posinf=255.0, neginf=0.0)
                arr = np.clip(arr, 0, 255).astype(np.uint8)
        except Exception:  # noqa: BLE001 - oddly shaped/garbage input
            return None

        return self._downscale(arr)

    def _downscale(self, arr):
        """Integer-block mean, dropping the ragged right/bottom edge pixels."""
        factor = self._downscale_factor
        if factor > 1:
            height, width = arr.shape
            rows, cols = height // factor, width // factor
            if rows > 0 and cols > 0:
                trimmed = arr[: rows * factor, : cols * factor]
                blocks = trimmed.reshape(rows, factor, cols, factor).mean(axis=(1, 3))
                return blocks.astype(np.float64, copy=False)
        # Small (or degenerate) frames are compared at full resolution.
        return arr.astype(np.float64, copy=False)

    # -------------------------------------------------------------- decisions
    def decide(self, gray, task_active=False, now=None):
        """Return the upload decision for one captured frame.

        Parameters
        ----------
        gray:
            Grayscale image, ``uint8`` ``H x W`` (other dtypes/2-D shapes are
            coerced, shapes may change between calls).
        task_active:
            ``True`` while a pose/action task is running.  This forces
            ``allowed=True`` and prevents any frame drop in an action window.
        now:
            Optional explicit timestamp (seconds); defaults to ``clock()``.

        Returns
        -------
        dict
            ``allowed`` (bool), ``reason`` (one of :data:`REASONS`),
            ``state`` (one of :data:`STATES`), ``changed_ratio`` (float),
            ``since_audit_s`` and ``since_last_allow_s`` (floats measured
            *before* this decision was applied, ``None`` on the first frame).
        """
        ts = float(self._clock()) if now is None else float(now)

        # FIX-03：证据过期/epoch 变化后不得继续 claim 空场景，退回 uncertain（放行）。
        if self._state == STATE_EMPTY_VERIFIED and not self._evidence_valid(ts):
            self._state = STATE_UNCERTAIN
            self._quiet_count = 0

        # Reported "time since last allow", captured before this call moves it.
        since_allow = (
            None if self._last_allow_ts is None else max(0.0, ts - self._last_allow_ts)
        )

        frame = self._prepare(gray)
        if frame is None:
            self._prev = None
            changed_ratio = 0.0
            has_reference = False
        else:
            previous = self._prev
            self._prev = frame
            if previous is not None and previous.shape == frame.shape:
                delta = np.abs(frame - previous)
                changed = int(np.count_nonzero(delta > self._diff_threshold))
                changed_ratio = changed / float(delta.size)
                has_reference = True
            else:
                # Different geometry (or no predecessor): no valid reference.
                changed_ratio = 0.0
                has_reference = False

        # 1. First frame (or the frame after a geometry change) -----------------
        if not has_reference:
            self._state = STATE_UNCERTAIN
            self._quiet_count = 0
            self._last_audit_ts = None  # the old audit no longer describes anything
            allowed, reason = True, REASON_FIRST_FRAME

        # 2. A task is running: never drop a frame, never stack up quiet time ---
        elif bool(task_active):
            self._state = STATE_ACTIVE
            self._quiet_count = 0
            allowed, reason = True, REASON_TASK_GUARD

        # 3./4. Real motion: back to active and let the frame through -----------
        elif changed_ratio >= self._active_motion_ratio:
            self._state = STATE_ACTIVE
            self._quiet_count = 0
            allowed, reason = True, REASON_ACTIVE_OBSERVE

        # 7. Verified empty: audit cadence (after the min-gap floor) ------------
        elif self._state == STATE_EMPTY_VERIFIED:
            # In this state the last allowed frame *is* the last audit.
            if since_allow is not None and since_allow + _EPS < self._min_gap_s:
                allowed, reason = False, REASON_MIN_GAP
            elif since_allow is not None and since_allow + _EPS >= self._audit_interval_s:
                self._last_audit_ts = ts
                allowed, reason = True, REASON_AUDIT_DUE
            else:
                allowed, reason = False, REASON_EMPTY_SCENE_SKIP

        # 6. Active but this frame is quiet: count towards re-verifying ---------
        elif self._state == STATE_ACTIVE:
            if changed_ratio < self._quiet_ratio:
                self._quiet_count += 1
            else:
                self._quiet_count = 0
            if (self._quiet_count >= self._confirm_quiet_frames
                    and self._evidence_valid(ts)):
                self._state = STATE_EMPTY_VERIFIED
                self._last_audit_ts = ts
                self._quiet_count = 0
            allowed, reason = True, REASON_ACTIVE_OBSERVE

        # 5. Uncertain: observe everything, promote after a quiet streak --------
        else:
            if changed_ratio < self._quiet_ratio:
                self._quiet_count += 1
            else:
                self._quiet_count = 0
            # The audit-interval guard only defers promotion when a *previous*
            # audit exists; a freshly booted Pi promotes after the quiet streak
            # (~0.6 s at 20 fps) so the feature starts paying off immediately.
            if (
                self._quiet_count >= self._confirm_quiet_frames
                and self._since_audit(ts) + _EPS >= self._audit_interval_s
                and self._evidence_valid(ts)
            ):
                self._state = STATE_EMPTY_VERIFIED
                self._last_audit_ts = ts
                self._quiet_count = 0
            allowed, reason = True, REASON_UNCERTAIN_OBSERVE

        if allowed:
            self._last_allow_ts = ts

        self._frames_seen += 1
        if allowed:
            self._allowed_count += 1
        else:
            self._skipped_count += 1
        self._by_reason[reason] = self._by_reason.get(reason, 0) + 1
        self._by_state[self._state] = self._by_state.get(self._state, 0) + 1

        return {
            "allowed": bool(allowed),
            "reason": reason,
            "state": self._state,
            "changed_ratio": float(changed_ratio),
            # Same quantity by contract: seconds since the last allowed frame.
            "since_audit_s": None if since_allow is None else float(since_allow),
            "since_last_allow_s": None if since_allow is None else float(since_allow),
        }

    # ------------------------------------------------------------------ reset
    def reset(self):
        """Forget the reference frame, timers, state and counters."""
        self._prev = None
        self._evidence_kind = "UNKNOWN"
        self._evidence_ts = None
        self._evidence_epoch = None
        self._state = STATE_UNCERTAIN
        self._quiet_count = 0
        self._last_allow_ts = None
        self._last_audit_ts = None
        self._frames_seen = 0
        self._allowed_count = 0
        self._skipped_count = 0
        self._by_reason = {}
        self._by_state = {}
