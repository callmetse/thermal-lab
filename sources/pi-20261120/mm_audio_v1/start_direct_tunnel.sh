#!/bin/bash
pkill -f "19001:127.0.0.1:19001" 2>/dev/null
sleep 1
setsid nohup ssh -N -i /home/pi/.ssh/mm_audio_direct -o BatchMode=yes -o StrictHostKeyChecking=accept-new   -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3   -L 127.0.0.1:19001:127.0.0.1:19001 -p 17005 root@116.136.52.182 >/tmp/direct_tunnel.log 2>&1 < /dev/null &
sleep 5
/home/pi/abc20261102/.venv/bin/python - <<'EOF'
import socket
try:
    s = socket.create_connection(('127.0.0.1', 19001), timeout=5)
    s.sendall(b'ping'); print('tunnel echo', s.recv(4)); s.close()
except Exception as e:
    print('tunnel failed:', e)
EOF
