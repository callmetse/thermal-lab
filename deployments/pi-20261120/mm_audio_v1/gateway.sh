#!/bin/sh
set -eu
set -a
. /home/pi/.config/abc20261102/console-auth.env
set +a
exec /home/pi/abc20261102/runtime/viewer/caddy run --config /home/pi/mm_audio_v1/Caddyfile --adapter caddyfile
