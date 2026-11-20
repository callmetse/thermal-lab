#!/bin/sh
# Independent environment service. Repeated calls do not create duplicate writers.
cd /home/pi/env_sensor_v1 || exit 1
exec 9>/home/pi/env_sensor_v1/service-start.lock
flock -n 9 || exit 0
ctl=/home/pi/abc20261102/.venv/bin/supervisorctl
cfg=/home/pi/env_sensor_v1/supervisord.ini
if "$ctl" -c "$cfg" status env-gateway >/dev/null 2>&1; then exit 0; fi
if "$ctl" -c "$cfg" pid >/dev/null 2>&1; then
  "$ctl" -c "$cfg" start env-gateway
else
  /home/pi/abc20261102/.venv/bin/supervisord -c "$cfg"
fi
flock -u 9
