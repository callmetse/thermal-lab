#!/bin/sh
# 在 Pi 上启动环境网关，并做一次自检。可重复执行（先停旧进程）。
set -e
cd /home/pi/env_sensor_v1
chmod +x run_env_gateway.sh

pkill -f 'env_gateway.py --host 0.0.0.0 --port 28984' 2>/dev/null || true
sleep 1
setsid nohup sh run_env_gateway.sh > /dev/null 2>&1 < /dev/null &
sleep 2

echo "== health =="
curl -s --max-time 5 http://127.0.0.1:28984/env/v1/health || echo "health failed"
echo
echo "== listening =="
ss -ltn | grep 28984 || echo "28984 not listening"
echo "== gateway.log tail =="
tail -n 5 gateway.log 2>/dev/null || echo "no log yet"

echo "== crontab @reboot =="
if crontab -l 2>/dev/null | grep -q 'env_sensor_v1/run_env_gateway.sh'; then
  echo "already present"
else
  ( crontab -l 2>/dev/null; echo '@reboot sh /home/pi/env_sensor_v1/run_env_gateway.sh' ) | crontab -
  echo "added"
fi
crontab -l 2>/dev/null | grep env_sensor_v1 || true
