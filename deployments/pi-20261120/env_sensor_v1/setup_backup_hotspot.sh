#!/bin/sh
set -eu
cd /home/pi/env_sensor_v1
sudo install -m 600 -o root -g root hotspot-backup.nmconnection /etc/NetworkManager/system-connections/env-backup-hotspot.nmconnection
sudo nmcli connection load /etc/NetworkManager/system-connections/env-backup-hotspot.nmconnection
nmcli -f connection.id,connection.autoconnect,connection.autoconnect-priority,802-11-wireless.ssid connection show env-backup-hotspot
printf '\nBackup saved; active Wi-Fi was not changed.\n'
