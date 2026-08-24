#!/usr/bin/env bash
# studio-service-swap.sh — batch-safe reconcile + apply for Hanzo Studio.
#
# Hanzo Studio runs as exactly ONE unit: the repo-canonical system unit
# ops/systemd/studio.service (installed at /etc/systemd/system/studio.service,
# serving :8188). Today's outage was caused by a DUPLICATE unit (a transient/
# user `hanzo-studio.service`) racing it for the port. This script enforces the
# single-unit invariant and rolls out code/unit changes WITHOUT interrupting an
# in-flight render batch:
#
#   1. wait until nothing is rendering
#   2. remove any stray duplicate units (user/transient hanzo-studio.service)
#   3. (re)install ops/systemd/studio.service -> /etc, enable it, daemon-reload
#   4. restart studio.service so it picks up the latest code + unit env
#   5. health-check :8188
#
# Idempotent; safe to re-run. Needs sudo for the system unit.
#
# Usage:
#   scripts/studio-service-swap.sh                 # wait for idle, then apply
#   STUDIO_SWAP_TIMEOUT=0 scripts/...              # apply immediately (skip wait)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO/ops/systemd/studio.service"
UNIT_DST="/etc/systemd/system/studio.service"
PORT="${STUDIO_PORT:-8188}"
STATUS_URL="http://127.0.0.1:${PORT}/prompt"
WAIT_TIMEOUT="${STUDIO_SWAP_TIMEOUT:-7200}"   # seconds to wait for an idle box (0 = don't wait)
POLL_INTERVAL="${STUDIO_SWAP_POLL:-15}"

log() { printf '[swap] %s\n' "$*"; }

render_state() {
  # -> busy | idle | down
  local body
  body="$(curl -fsS -m 5 "$STATUS_URL" 2>/dev/null)" || { echo down; return; }
  python3 - "$body" <<'PY'
import json, sys
try:
    print("busy" if json.loads(sys.argv[1])["exec_info"]["rendering"] else "idle")
except Exception:
    print("down")
PY
}

wait_for_idle() {
  [ "$WAIT_TIMEOUT" -eq 0 ] && { log "STUDIO_SWAP_TIMEOUT=0 -> applying without waiting"; return; }
  local waited=0 st
  while :; do
    st="$(render_state)"
    case "$st" in
      idle) log "nothing rendering -> proceeding"; return ;;
      down) log "server not reachable -> proceeding"; return ;;
      busy) log "rendering; ${waited}s/${WAIT_TIMEOUT}s elapsed, recheck in ${POLL_INTERVAL}s" ;;
    esac
    if (( waited >= WAIT_TIMEOUT )); then
      log "ERROR: still rendering after ${WAIT_TIMEOUT}s; aborting to protect the render"; exit 1
    fi
    sleep "$POLL_INTERVAL"; waited=$((waited + POLL_INTERVAL))
  done
}

wait_for_idle

# 2. Kill off any duplicate units that could race :8188.
if systemctl --user list-units --all 2>/dev/null | grep -q 'hanzo-studio\.service'; then
  log "removing stray user hanzo-studio.service"
  systemctl --user disable --now hanzo-studio.service 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/hanzo-studio.service" \
        "$HOME/.config/systemd/user/default.target.wants/hanzo-studio.service"
  systemctl --user daemon-reload || true
fi

# 3. Install/refresh the canonical system unit from the repo.
if ! sudo cmp -s "$UNIT_SRC" "$UNIT_DST" 2>/dev/null; then
  log "installing canonical unit: $UNIT_SRC -> $UNIT_DST"
  sudo install -m 0644 "$UNIT_SRC" "$UNIT_DST"
fi
sudo systemctl enable studio.service
sudo systemctl daemon-reload

# 4. Restart to pick up latest code + unit env (nothing is rendering at this point).
log "restarting studio.service"
sudo systemctl restart studio.service

# 5. Health-check.
log "waiting for :$PORT ..."
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "$QUEUE_URL" >/dev/null 2>&1; then
    log "OK — studio.service is serving on :$PORT (single canonical unit)"
    exit 0
  fi
  sleep 3
done
log "ERROR: server did not answer on :$PORT within 180s"
journalctl -u studio.service -n 40 --no-pager || true
exit 1
