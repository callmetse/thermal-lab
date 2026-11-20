#!/bin/sh
cd /home/pi/mm_audio_v1 || exit 1
exec 9>/home/pi/mm_audio_v1/start-service.lock
flock -n 9 || exit 0
ctl=/home/pi/abc20261102/.venv/bin/supervisorctl
cfg=/home/pi/mm_audio_v1/supervisord.ini
if ! "$ctl" -c "$cfg" pid >/dev/null 2>&1; then
 /home/pi/abc20261102/.venv/bin/supervisord -c "$cfg"
fi
flock -u 9
