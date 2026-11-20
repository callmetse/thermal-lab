"""Bounded local uploads. Never accept client paths or remote source URLs.

P1-07：除单文件 512 MB 上限外，`data/uploads` 还有总量配额（`upload_quota_bytes`，
默认 2 GiB）；超配额在写盘前拒绝，并在流式接收中随时复核。启动时清理上次进程
留下的 `.part` 半成品（单上传锁保证残留一定是废的）。
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid
from aiohttp import web
from .dataset_settings import human_bytes, read_settings

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 512 * 1024 * 1024
DEFAULT_UPLOAD_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
LOW_DISK_RESERVE_BYTES = 256 * 1024 * 1024


def upload_folder():
    return ROOT / 'data/uploads'


def upload_quota_bytes():
    """配额优先级：环境变量 ABC_UPLOAD_QUOTA_BYTES > config.json 的 upload_quota_bytes > 默认值。"""
    override = os.environ.get('ABC_UPLOAD_QUOTA_BYTES')
    if override:
        try:
            value = int(float(override))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        value = int(read_settings(ROOT).get('upload_quota_bytes', DEFAULT_UPLOAD_QUOTA_BYTES))
        if value > 0:
            return value
    except Exception:  # noqa: BLE001 - 配置不可读时退回默认配额，不能因此拒绝服务
        pass
    return DEFAULT_UPLOAD_QUOTA_BYTES


def uploads_bytes(folder=None):
    """`data/uploads` 现占字节数（视频、元数据、残片都算，口径保守）。"""
    folder = Path(folder) if folder is not None else upload_folder()
    total = 0
    if folder.is_dir():
        for path in folder.iterdir():
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def purge_partials(folder=None):
    """删掉上次进程留下的 `.part`；返回删除个数。"""
    folder = Path(folder) if folder is not None else upload_folder()
    removed = 0
    if folder.is_dir():
        for path in folder.glob('*.part'):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def quota_message(used, quota, incoming):
    """配额判定的唯一口径：允许返回 None，拒绝返回可直接展示的中文原因。"""
    quota = int(quota)
    used = int(used)
    incoming = int(incoming)
    remaining = max(0, quota - used)
    if incoming <= remaining:
        return None
    return ('上传总量已超配额：已用 %s / 配额 %s，本次需要 %s（剩余 %s）；'
            '请先清理旧视频，占用明细见 python tools/capacity_report.py'
            % (human_bytes(used), human_bytes(quota), human_bytes(incoming), human_bytes(remaining)))


def video_path(upload_id):
    if not isinstance(upload_id, str) or not re.fullmatch(r'[a-f0-9]{32}', upload_id):
        raise ValueError('无效的视频编号')
    p = ROOT / 'data/uploads' / (upload_id + '.video')
    if not p.is_file() or not p.with_suffix('.json').is_file():
        raise ValueError('视频不存在，请重新上传')
    return p


def inspect_video(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        ok, image = cap.read()
        if not ok or not math.isfinite(fps) or fps <= 0 or not math.isfinite(frames) or frames <= 0:
            raise ValueError('无法读取视频，请使用 MP4（H.264）格式')
        duration = frames / fps
        if duration > 3600:
            raise ValueError('单个视频最长 60 分钟，请先裁剪')
        return dict(duration_s=duration, fps=fps, width=image.shape[1], height=image.shape[0])
    finally:
        cap.release()


def _write_chunk(handle, chunk):
    """在工作线程里落盘：SD 卡抖动不能阻塞 aiohttp 的 event loop（P2-11）。"""
    handle.write(chunk)


def install(server, app):
    lock = asyncio.Lock()
    folder = upload_folder()
    folder.mkdir(parents=True, exist_ok=True)
    removed = purge_partials(folder)
    if removed:
        print('uploads: 启动清理 %d 个 .part 残留' % removed, flush=True)

    async def upload(request):
        if lock.locked():
            return web.json_response({'error': '正在接收另一个视频，请稍后'}, status=409)
        async with lock:
            token = uuid.uuid4().hex
            part = folder / (token + '.part')
            target = folder / (token + '.video')
            try:
                declared = request.content_length or 0
                if declared > MAX_BYTES:
                    raise ValueError('视频不能超过 512 MB')
                quota = upload_quota_bytes()
                used = uploads_bytes(folder)
                over = quota_message(used, quota, declared)
                if over:
                    raise ValueError(over)
                reserve = min(declared, MAX_BYTES) if declared else MAX_BYTES
                if shutil.disk_usage(folder).free < reserve + LOW_DISK_RESERVE_BYTES:
                    raise ValueError('存储空间不足，请联系管理员整理上传视频')
                total = 0
                digest = hashlib.sha256()
                loop = asyncio.get_running_loop()
                with part.open('wb') as out:
                    async for chunk in request.content.iter_chunked(256 * 1024):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError('视频不能超过 512 MB')
                        over = quota_message(used, quota, total)
                        if over:
                            raise ValueError(over)
                        digest.update(chunk)
                        await loop.run_in_executor(None, _write_chunk, out, chunk)
                meta = await loop.run_in_executor(None, inspect_video, part)
                meta.update(upload_id=token, bytes=total, sha256=digest.hexdigest())
                part.replace(target)
                target.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
                return web.json_response(meta, status=201)
            except ValueError as e:
                return web.json_response({'error': str(e)}, status=400)
            finally:
                if part.exists():
                    part.unlink()

    app.router.add_post('/api/video-uploads', upload)

    async def data_page(request):
        return web.FileResponse(ROOT / 'deploy/nodered/data.html')
    app.router.add_get('/data', data_page)
