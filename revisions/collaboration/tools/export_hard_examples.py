"""把 Pi 上的难例暂存箱导出成"可标注包"（W1 → W2 的交接面）。

用法（在 Pi 上）：
    .venv/bin/python tools/export_hard_examples.py --out state/hard_examples_outbox
    .venv/bin/python tools/export_hard_examples.py --out … --no-verify

产物（自包含，可整体 scp 回 PC 交给标注）：
    manifest.jsonl        每条一行：元数据 + 校验后的 media_sha256
    labels_template.csv   与 data/labels_template.csv 同一 schema 的空标注表
    dataset_manifest.json 版本、条数、按理由分布、校验结果、导出时间
    samples/<id>.jpg      原图副本（--no-copy 可跳过，只留清单）

原则：导出只读、不删源；校验失败的样本不静默丢弃，而是在 dataset_manifest.json 里点名。
"""
import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if (ROOT / 'sample_store.py').is_file():
    from sample_store import BoundedSampleStore
else:
    from lab.sample_store import BoundedSampleStore

LABEL_COLUMNS = [
    'source_sha256', 'start_frame', 'end_frame', 'start_s', 'end_s', 'manual_person_id',
    'bbox_xyxy', 'visible_clothing', 'posture_gt', 'action_gt', 'occlusion',
    'complete_ensemble_confirmed', 'ensemble_description', 'task_and_intensity',
    'reference_kind', 'clo_reference', 'met_reference', 'reference_source',
    'annotator', 'notes',
]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--store', type=Path, default=ROOT / 'state' / 'hard_examples')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--no-verify', action='store_true', help='跳过 SHA256 复核（不推荐）')
    parser.add_argument('--no-copy', action='store_true', help='只写清单，不复制图片')
    args = parser.parse_args(argv)

    if not args.store.exists():
        print(f'暂存箱不存在：{args.store}', file=sys.stderr)
        return 2
    store = BoundedSampleStore(args.store)
    entries = store.scan()
    if not entries:
        print(f'暂存箱是空的：{args.store}')
        return 1

    out = args.out
    (out / 'samples').mkdir(parents=True, exist_ok=True)
    verified, corrupted = [], []
    for entry in entries:
        image = args.store / entry['media_uri']
        digest = sha256_file(image)
        ok = digest == entry.get('media_sha256')
        if not ok and not args.no_verify:
            corrupted.append({'sample_id': entry['sample_id'], 'expected': entry.get('media_sha256'),
                              'actual': digest})
            continue
        record = {**entry, 'verified_sha256': digest, 'verified': ok}
        verified.append(record)
        if not args.no_copy:
            shutil.copy2(image, out / 'samples' / f"{entry['sample_id']}.jpg")

    with (out / 'manifest.jsonl').open('w', encoding='utf-8') as handle:
        for record in verified:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')

    with (out / 'labels_template.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(LABEL_COLUMNS)
        for record in verified:
            writer.writerow([record['media_sha256'], (record.get('source_frame_ids') or [None])[0], '', '',
                             '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''])

    by_reason, by_arm, by_gate = {}, {}, {}
    for record in verified:
        for reason in record.get('trigger_reasons') or []:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        by_arm[record.get('arm')] = by_arm.get(record.get('arm'), 0) + 1
        by_gate[record.get('gate_state')] = by_gate.get(record.get('gate_state'), 0) + 1
    summary = {
        'schema_version': 'hard_example_dataset_v0',
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'store': str(args.store),
        'version': time.strftime('%Y%m%d'),
        'samples': len(verified),
        'corrupted': corrupted,
        'by_reason': by_reason, 'by_arm': by_arm, 'by_gate': by_gate,
        'store_stats': store.stats(),
        'notes': [
            '这是暂存箱导出，不是冻结测试集；split 全部为 unassigned。',
            'trigger_reasons 记录的是"为什么采"，不能直接当标签。',
            '标签列沿用 data/labels_template.csv 的 20 列 schema。',
        ],
    }
    (out / 'dataset_manifest.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'samples': len(verified), 'corrupted': len(corrupted),
                      'by_reason': by_reason, 'out': str(out)}, ensure_ascii=False))
    return 0 if not corrupted else 3


if __name__ == '__main__':
    raise SystemExit(main())
