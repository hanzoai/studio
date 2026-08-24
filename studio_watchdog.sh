#!/usr/bin/env bash
# Studio watchdog: keep :8188 alive through the overnight karma render run.
# Emits a line ONLY on a restart event (so it's a signal, not a firehose).
# Detects: process gone, port unresponsive, or output-log silent >12min while a
# render is supposedly in flight (the 6h-hang failure mode from the QC log).
set -u
STUDIO=/home/z/work/hanzo/studio
LOG=$STUDIO/studio-local.log
launch() {
  cd "$STUDIO"
  # Free the port first: a stale/duplicate main.py holding :8188 makes the new
  # one crash on bind (EADDRINUSE). Kill any existing listener, wait for release.
  pkill -f 'main.py --listen' 2>/dev/null || true
  for _ in $(seq 1 15); do
    ss -ltn 2>/dev/null | grep -q ':8188 ' || break
    sleep 1
  done
  HF_HOME=/home/z/.cache/huggingface \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  nohup .venv/bin/python main.py --listen 0.0.0.0 --port 8188 --normalvram \
    --disable-auto-launch --output-directory "$STUDIO/output" >> "$LOG" 2>&1 &
  sleep 1
  local pid
  pid=$(pgrep -f 'main.py --listen' | head -1)
  [ -n "$pid" ] && sudo -n sh -c "echo -700 > /proc/$pid/oom_score_adj" 2>/dev/null || true
  echo "$pid"
}
while true; do
  code=$(curl -s -m 5 http://127.0.0.1:8188/system_stats -o /dev/null -w "%{http_code}" 2>/dev/null || echo 000)
  if [ "$code" != "200" ]; then
    # give it one grace re-check (could be mid-render, momentarily busy)
    sleep 8
    code=$(curl -s -m 5 http://127.0.0.1:8188/system_stats -o /dev/null -w "%{http_code}" 2>/dev/null || echo 000)
    if [ "$code" != "200" ]; then
      pid=$(launch)
      # wait for it to answer
      for i in $(seq 1 45); do
        c=$(curl -s -m 4 http://127.0.0.1:8188/system_stats -o /dev/null -w "%{http_code}" 2>/dev/null || echo 000)
        [ "$c" = "200" ] && break
        sleep 4
      done
      echo "STUDIO_RESTARTED pid=$pid health=$c"
    fi
  fi
  sleep 45
done
