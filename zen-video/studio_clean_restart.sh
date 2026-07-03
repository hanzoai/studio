#!/usr/bin/env bash
# Hard-clean restart of studio.service: break orphan/EADDRINUSE crash loop.
set -u
echo "[1] stop service"
sudo systemctl stop studio.service 2>&1
sleep 2
echo "[2] kill all studio python + free 8188 (repeat until clear)"
for i in 1 2 3 4 5; do
  sudo fuser -k 8188/tcp 2>/dev/null
  pkill -9 -f "studio/.venv/bin/python main.py" 2>/dev/null
  sleep 2
  holders=$(sudo ss -tlnp 2>/dev/null | grep -c ':8188 ')
  procs=$(pgrep -c -f "studio/.venv/bin/python main.py" 2>/dev/null || echo 0)
  echo "  pass=$i holders=$holders procs=$procs"
  if [ "$holders" = "0" ] && [ "$procs" = "0" ]; then break; fi
done
echo "[3] confirm 8188 truly dead"
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8188/ 2>/dev/null)
echo "  curl 8188 -> $code (000 = free)"
echo "[4] mem"
free -g | awk '/Mem:/{print "  free="$4"GB used="$3"GB"}'
echo "[5] reset-failed + start"
sudo systemctl reset-failed studio.service
sudo systemctl start studio.service
echo "  start rc=$? state=$(systemctl is-active studio.service)"
