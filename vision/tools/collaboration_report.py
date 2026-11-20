"""把 state/collaboration/*.jsonl 汇总成"今天链路发生了什么"。

用法（在 Pi 上）：
    .venv/bin/python tools/collaboration_report.py              # 今天（按文件名日期）
    .venv/bin/python tools/collaboration_report.py --days 3
    .venv/bin/python tools/collaboration_report.py --json

设计口径：
- 只读本地 jsonl，不连 MQTT、不查服务；断网也能看。
- "媒体字节"用 Pi 侧 frame_bytes / duration_s；结果速率用协议里的 observation 计数
  （边缘侧看不到云端结果数，这里只汇总策略与门控）。
- 缺失字段不补零，宁可显示 None，避免把"没记录"读成"跑了 0"。
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / 'state' / 'collaboration'
JST = timezone(timedelta(hours=9))


def load(directory, days=1, today=None):
    today = today or datetime.now(JST).date()
    keep = {str(today - timedelta(days=i)) for i in range(days)}
    records = []
    for path in sorted(directory.glob('*.jsonl')):
        if path.stem not in keep:
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record['_date'] = path.stem
            records.append(record)
    return records


def summarize(records):
    sessions = len(records)
    total_s = sum(r.get('duration_s') or 0 for r in records)
    bytes_total = sum(r.get('frame_bytes') or 0 for r in records)
    drops = {}
    for r in records:
        for reason, count in (r.get('dropped') or {}).items():
            drops[reason] = drops.get(reason, 0) + int(count)
    gate = {'allowed': 0, 'skipped': 0, 'by_state': {}}
    for r in records:
        g = r.get('scene_gate') or {}
        gate['allowed'] += int(g.get('allowed') or 0)
        gate['skipped'] += int(g.get('skipped') or 0)
        for state, count in (g.get('by_state') or {}).items():
            gate['by_state'][state] = gate['by_state'].get(state, 0) + int(count)
    transitions = []
    for r in records:
        for t in (r.get('health') or {}).get('transitions') or []:
            transitions.append({'run_id': r.get('run_id'), **t})
    modes = {}
    for r in records:
        mode = r.get('mode') or 'off'
        modes[mode] = modes.get(mode, 0) + 1
    return {
        'sessions': sessions,
        'duration_s': round(total_s, 1),
        'media_mbps': round(bytes_total * 8 / total_s / 1e6, 3) if total_s else None,
        'modes': modes,
        'drops': drops,
        'scene_gate': gate,
        'health_transitions': len(transitions),
        'recoveries_s': [r['health']['recovery_s'] for r in records
                         if (r.get('health') or {}).get('recovery_s') is not None],
        'fallback_packets': sum(int(r.get('preview_fallback_packets') or 0) for r in records),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dir', type=Path, default=DEFAULT_DIR)
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if not args.dir.exists():
        print(f'还没有观测目录：{args.dir}（跑一轮会话后会自动生成）')
        return 1
    records = load(args.dir, args.days)
    summary = summarize(records)
    if args.json:
        print(json.dumps({'summary': summary, 'records': records}, ensure_ascii=False, indent=2))
        return 0
    if not records:
        print(f'{args.days} 天内没有会话记录（{args.dir}）')
        return 1
    print(f"会话 {summary['sessions']} 轮 · 累计 {summary['duration_s']} s · "
          f"媒体 {summary['media_mbps']} Mbps · 模式 {summary['modes']}")
    print(f"丢弃分桶：{summary['drops'] or '{}'}")
    print(f"场景门控：放行 {summary['scene_gate']['allowed']} / 拦截 {summary['scene_gate']['skipped']} "
          f"· 状态 {summary['scene_gate']['by_state']}")
    print(f"健康状态机：{summary['health_transitions']} 次迁移 · "
          f"恢复时间 {summary['recoveries_s']} · 离线兜底预览帧 {summary['fallback_packets']}")
    print('\n逐轮：')
    for r in records:
        print(f"  {r.get('_date')} {r.get('run_id','')[:8]} arm={r.get('arm')} "
              f"{r.get('duration_s')}s media={round((r.get('frame_bytes') or 0)*8/(r.get('duration_s') or 1)/1e6,2)}Mbps "
              f"mode={r.get('mode')} sent={r.get('sent_frames')} drops={r.get('dropped') or {}}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
