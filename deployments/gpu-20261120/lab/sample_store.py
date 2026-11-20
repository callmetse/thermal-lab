"""有界难例存储（W1）：三限（字节 / 条数 / 年龄）+ 内容哈希去重 + 原子落盘。

设计约束（来自复核文档 §4.3）：
- 三限同时生效，按"最旧且未锁定"淘汰；人工已标注/冻结的样本不得被清理器删除；
- 先写临时文件 → 校验 → 原子改名，不能出现"元数据在、图片不在"；
- 内容哈希即文件名：同一帧重复采集只留一份（静坐的人不会刷出几千条近重复样本）；
- 存储只是"暂存箱"，持久化与数据集版本在 GPU/PC 侧另行管理。
"""
import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 'hard_example_v1'


def _safe_unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


class BoundedSampleStore:
    def __init__(self, directory, max_bytes=50 * 1024 * 1024, max_samples=500,
                 max_age_s=7 * 24 * 3600, clock=time.time):
        self.root = Path(directory)
        self.samples = self.root / 'samples'
        self.samples.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.max_samples = int(max_samples)
        self.max_age_s = float(max_age_s)
        self.clock = clock
        self.counters = {'saved': 0, 'duplicate': 0, 'rejected': 0, 'evicted': 0}
        self.reasons = {}

    # -- 写入 -------------------------------------------------------------
    def put(self, jpeg, meta):
        """保存一帧；返回 {'status': saved|duplicate|rejected, 'sample_id':…}。"""
        if not jpeg:
            self.counters['rejected'] += 1
            return {'status': 'rejected', 'sample_id': None, 'why': 'empty'}
        digest = hashlib.sha256(jpeg).hexdigest()
        sample_id = digest[:16]
        image = self.samples / f'{sample_id}.jpg'
        sidecar = self.samples / f'{sample_id}.json'
        for reason in meta.get('trigger_reasons') or []:
            self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if image.exists():
            self.counters['duplicate'] += 1
            return {'status': 'duplicate', 'sample_id': sample_id}

        record = {**meta, 'schema_version': SCHEMA_VERSION, 'sample_id': sample_id,
                  'media_sha256': digest,
                  'media_uri': f'samples/{sample_id}.jpg',
                  'media_bytes': len(jpeg),
                  'captured_at': round(self.clock(), 3),
                  'label_status': meta.get('label_status', 'unlabeled'),
                  'dataset_split': meta.get('dataset_split', 'unassigned')}

        tmp_image = self.samples / f'.{sample_id}.jpg.tmp'
        tmp_meta = self.samples / f'.{sample_id}.json.tmp'
        try:
            tmp_image.write_bytes(jpeg)
            if hashlib.sha256(tmp_image.read_bytes()).hexdigest() != digest:
                raise ValueError('hash mismatch after write')
            tmp_meta.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp_image, image)
            os.replace(tmp_meta, sidecar)
        except (OSError, ValueError) as error:
            _safe_unlink(tmp_image)
            _safe_unlink(tmp_meta)
            self.counters['rejected'] += 1
            return {'status': 'rejected', 'sample_id': None, 'why': repr(error)}

        self.counters['saved'] += 1
        self.prune()
        return {'status': 'saved', 'sample_id': sample_id}

    # -- 清理 -------------------------------------------------------------
    def prune(self, now=None):
        """按三限淘汰最旧且未锁定的样本；返回被删条数。"""
        now = self.clock() if now is None else now
        entries = self.scan()
        total_bytes = sum(e['media_bytes'] for e in entries)
        removed = 0
        for entry in entries:                      # scan() 按 captured_at 升序
            over = (total_bytes > self.max_bytes or len(entries) > self.max_samples
                    or (now - entry['captured_at']) > self.max_age_s)
            if not over:
                break
            if entry.get('locked') or entry.get('label_status') != 'unlabeled':
                continue                           # 已标注/冻结的永不自动清理
            _safe_unlink(self.root / entry['media_uri'])
            _safe_unlink(self.samples / f"{entry['sample_id']}.json")
            total_bytes -= entry['media_bytes']
            entries = [e for e in entries if e['sample_id'] != entry['sample_id']]
            removed += 1
        self.counters['evicted'] += removed
        return removed

    # -- 读 ---------------------------------------------------------------
    def scan(self):
        """返回按 captured_at 升序的元数据列表（跳过损坏文件）。"""
        entries = []
        for sidecar in self.samples.glob('*.json'):
            try:
                entry = json.loads(sidecar.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            image = self.root / entry.get('media_uri', '')
            if not image.exists():
                continue
            entry['media_bytes'] = entry.get('media_bytes') or image.stat().st_size
            entries.append(entry)
        entries.sort(key=lambda e: (e.get('captured_at') or 0, e.get('sample_id') or ''))
        return entries

    def stats(self):
        entries = self.scan()
        return {
            'samples': len(entries),
            'bytes': sum(e['media_bytes'] for e in entries),
            'max_bytes': self.max_bytes, 'max_samples': self.max_samples,
            'max_age_s': self.max_age_s,
            'by_reason': dict(self.reasons),
            'counters': dict(self.counters),
            'unlabeled': sum(1 for e in entries if e.get('label_status') == 'unlabeled'),
        }
