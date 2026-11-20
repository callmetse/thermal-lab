#!/bin/sh
# 幂等地把网关加进用户 crontab（@reboot），不需要 sudo。
set -u
TMP=$(mktemp)
crontab -l 2>/dev/null > "$TMP" || true
if grep -q 'env_sensor_v1/run_env_gateway.sh' "$TMP"; then
  echo "already present"
else
  echo '@reboot sleep 20; sh /home/pi/env_sensor_v1/run_env_gateway.sh' >> "$TMP"
  if crontab "$TMP" 2>&1; then
    echo "installed"
  else
    echo "install failed"
  fi
fi
rm -f "$TMP"
echo "--- crontab now ---"
crontab -l 2>&1 || echo "(empty)"
