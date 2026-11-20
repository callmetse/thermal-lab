#!/bin/bash
rm -f /tmp/utt_listener.jsonl
cd /home/pi/mm_audio_v1
pkill -f utt_listener.py 2>/dev/null
sleep 1
setsid nohup .venv/bin/python utt_listener.py >/tmp/utt_listener.log 2>&1 < /dev/null &
sleep 3
echo "listener: $(cat /tmp/utt_listener.log | tr '
' ' ')"
