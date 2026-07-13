#!/usr/bin/env bash
# install_custom_nodes.sh -- vendor the Hanzo Studio custom-node packs.
#
# Reads custom_nodes/hanzo-packs.txt, clones each pack at its pinned commit (git,
# idempotent), then installs their hand-resolved Python deps
# (custom_nodes-requirements.txt) with the studio torch/numpy/transformers stack
# LOCKED so nothing gets downgraded. The lock is DERIVED FROM THE LIVE ENV (not a
# hardcoded file), so it is correct in both contexts:
#   - Docker image build (CPU torch): STUDIO_PY=python  pip install
#   - Local uv venv (GB10, cu130 torch):
#       STUDIO_PY=.venv/bin/python \
#       STUDIO_PIP="uv pip install --python .venv/bin/python" \
#       scripts/install_custom_nodes.sh
#
# Upstream `comfy.*` imports are aliased to `studio.*` at boot by studio_compat,
# so packs run unmodified. Idempotent; safe to re-run to reconcile pins.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CN="$REPO/custom_nodes"
MANIFEST="$CN/hanzo-packs.txt"
REQS="$REPO/custom_nodes-requirements.txt"
PY="${STUDIO_PY:-python}"
PIP="${STUDIO_PIP:-python -m pip install}"
CGEN="$(mktemp)"
trap 'rm -f "$CGEN"' EXIT

log() { printf '[packs] %s\n' "$*"; }

# 0. Derive a constraints file locking the studio-critical packages to whatever
#    versions are ALREADY installed, so pack deps can never move the torch stack,
#    NumPy 2.x or Transformers 5.x. A dep that is incompatible surfaces as a
#    resolver error instead of silently breaking the runtime.
"$PY" - > "$CGEN" <<'PY'
import importlib.metadata as md
CRIT = ["torch", "torchvision", "torchaudio", "torchsde", "numpy",
        "transformers", "tokenizers", "pillow", "scipy", "timm",
        "safetensors", "huggingface-hub"]
for name in CRIT:
    try:
        print(f"{name}=={md.version(name)}")
    except md.PackageNotFoundError:
        pass
PY
log "locked stack:"; sed 's/^/[packs]   /' "$CGEN"

# 1. Clone / pin every pack from the manifest (comment + blank lines skipped),
#    then apply any local compat patches (scripts/patches/<pack>-*.patch). The
#    forced checkout discards previously-applied patches so the pin stays
#    authoritative and re-runs are idempotent.
log "reconciling packs from $MANIFEST"
while read -r name url sha _rest; do
  case "$name" in ''|\#*) continue ;; esac
  dir="$CN/$name"
  if [ -d "$dir/.git" ]; then
    git -C "$dir" fetch -q origin "$sha" 2>/dev/null || git -C "$dir" fetch -q --all --tags
  else
    git clone -q --filter=blob:none "$url" "$dir"
  fi
  git -C "$dir" checkout -qf "$sha"
  log "pinned $name @ $(git -C "$dir" rev-parse --short HEAD)"
  for p in "$REPO/scripts/patches/$name"-*.patch; do
    [ -f "$p" ] || continue
    git -C "$dir" apply "$p"
    log "applied $(basename "$p")"
  done
done < "$MANIFEST"

# 2. Install pack dependencies with the derived stack lock.
log "installing pack dependencies"
$PIP -c "$CGEN" -r "$REQS"

# 3. ultralytics (AGPL detectors for Impact-Subpack): --no-deps so it cannot pull
#    the non-headless opencv-python that would fight opencv-*-headless.
log "installing ultralytics (--no-deps)"
$PIP -c "$CGEN" --no-deps ultralytics

log "done -- $(grep -cvE '^\s*(#|$)' "$MANIFEST") packs pinned"
