#!/bin/bash
# 录 Pi 本地预览帧：先等画面出现，再录固定时长，最后合成 GIF 与 MP4。
# 用法: ./rec_cam.sh <录制秒数> <输出名> [等待上限秒]
SECS="${1:-14}"
NAME="${2:-bench}"
WAITMAX="${3:-180}"
DIR="/home/pi/mm_audio_v1/data/rec_$NAME"
URL="http://127.0.0.1:28990/api/preview-frame"
rm -rf "$DIR"; mkdir -p "$DIR"

echo "[wait] 等待会话画面出现（最多 ${WAITMAX}s）…"
waited=0
while [ "$waited" -lt "$WAITMAX" ]; do
  curl -s -o /tmp/w.jpg --max-time 1 "$URL" 2>/dev/null
  if [ -s /tmp/w.jpg ] && [ "$(stat -c%s /tmp/w.jpg 2>/dev/null || echo 0)" -gt 2000 ]; then
    echo "[wait] 有画面了，开始录 ${SECS}s"
    break
  fi
  sleep 0.5; waited=$((waited+1))
done
if [ "$waited" -ge "$WAITMAX" ]; then echo "[wait] 超时，没等到画面"; exit 3; fi

END=$(( $(date +%s) + SECS ))
ok=0; i=0
while [ "$(date +%s)" -lt "$END" ]; do
  i=$((i+1))
  f="$DIR/f_$(printf '%04d' $i).jpg"
  curl -s -o "$f" --max-time 1 "$URL" 2>/dev/null
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "$sz" -gt 2000 ]; then ok=$((ok+1)); else rm -f "$f"; fi
  sleep 0.09
done
echo "[rec] attempts=$i saved=$ok"

if [ "$ok" -lt 10 ]; then echo "[rec] 帧太少（$ok），不合成"; exit 2; fi

ffmpeg -y -loglevel error -framerate 10 -pattern_type glob -i "$DIR/*.jpg" \
  -vf "scale=520:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160[p];[b][p]paletteuse=dither=sierra2_4a" \
  "/home/pi/mm_audio_v1/results/bench_$NAME.gif"
ffmpeg -y -loglevel error -framerate 10 -pattern_type glob -i "$DIR/*.jpg" \
  -c:v libx264 -pix_fmt yuv420p -crf 20 \
  "/home/pi/mm_audio_v1/results/bench_$NAME.mp4"

ls -lh "/home/pi/mm_audio_v1/results/bench_$NAME.gif" "/home/pi/mm_audio_v1/results/bench_$NAME.mp4"
