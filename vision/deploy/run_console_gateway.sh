#!/bin/sh
set -eu
set -a
. /home/pi/.config/abc20261102/console-auth.env
set +a
exec /home/pi/abc20261102/runtime/viewer/caddy run --config /home/pi/abc20261102/deploy/console.Caddyfile --adapter caddyfile
