"""统一聚合口径（P2-09）。

画面侧的 experiment_archive 与环境侧的 env_longterm 必须用同一套规则解释"哪些样本参与均值"，
否则同一时段会算出两个数。规则带版本号，写进所有产出的统计里。
"""

RULE = {
    "version": "quality-ok-v1",
    "include_quality": ["ok"],
    "excluded_quality": ["stale", "invalid", "missing", "error", "unknown"],
    "mean": "sample",
    "time_weighted": False,
    "notes": "只用 quality=ok 的样本；样本均值（不是时间加权），未做校准验证",
}


def is_valid(quality):
    """未知 quality 一律不算有效样本（宁可少算，不可混算）。"""
    return quality in RULE["include_quality"]
