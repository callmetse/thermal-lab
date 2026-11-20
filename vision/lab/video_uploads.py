"""Bounded local uploads. Never accept client paths or remote source URLs."""
import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import uuid
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 512 * 1024 * 1024

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

def install(server, app):
    lock = asyncio.Lock()
    async def upload(request):
        if lock.locked():
            return web.json_response({'error':'正在接收另一个视频，请稍后'},status=409)
        async with lock:
            folder = ROOT / 'data/uploads'; folder.mkdir(parents=True,exist_ok=True)
            token = uuid.uuid4().hex; part=folder/(token+'.part'); target=folder/(token+'.video')
            try:
                if request.content_length and request.content_length > MAX_BYTES:
                    raise ValueError('视频不能超过 512 MB')
                if shutil.disk_usage(folder).free < MAX_BYTES + 512*1024*1024:
                    raise ValueError('存储空间不足，请联系管理员整理上传视频')
                total=0; digest=hashlib.sha256()
                with part.open('wb') as out:
                    async for chunk in request.content.iter_chunked(256*1024):
                        total+=len(chunk)
                        if total>MAX_BYTES: raise ValueError('视频不能超过 512 MB')
                        out.write(chunk);digest.update(chunk)
                meta=await asyncio.get_running_loop().run_in_executor(None,inspect_video,part)
                meta.update(upload_id=token,bytes=total,sha256=digest.hexdigest())
                part.replace(target)
                target.with_suffix('.json').write_text(json.dumps(meta),encoding='utf-8')
                return web.json_response(meta,status=201)
            except ValueError as e:
                return web.json_response({'error':str(e)},status=400)
            finally:
                if part.exists():part.unlink()
    app.router.add_post('/api/video-uploads',upload)
    async def data_page(request):
        return web.FileResponse(ROOT/'deploy/nodered/data.html')
    app.router.add_get('/data',data_page)
