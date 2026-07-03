#!/bin/bash
# Apply Hanzo Studio branding to the prebuilt frontend package (comfyui-frontend-package).
# Runs during the Docker build after pip install, and can be run against a venv
# for local dev. Idempotent: safe to run more than once.
set -e

STATIC_DIR=$(python3 -c "import comfyui_frontend_package, importlib.resources; print(str(importlib.resources.files(comfyui_frontend_package) / 'static'))")
BRANDING_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Hanzo Studio Branding ==="
echo "Target: $STATIC_DIR"

# --- 1. Replace in-app logos ---
echo "[1/5] Replacing logos..."
for logo_file in comfy-logo-single.svg comfy-logo-mono.svg comfy-cloud-logo.svg; do
    target="$STATIC_DIR/assets/images/$logo_file"
    if [ -f "$target" ]; then
        cp "$BRANDING_DIR/hanzo-logo.svg" "$target"
        echo "  Replaced $logo_file"
    else
        echo "  Not found: $logo_file (skipping)"
    fi
done

# --- 2. Install favicon (Hanzo mark) ---
# The frontend sets the runtime favicon to /assets/favicon.ico via useFavicon();
# the web root copies cover the pre-boot page load and the default /favicon.ico
# request. favicon.ico is a committed artifact rendered from favicon.svg by
# make_favicon.py (the Docker image has no rasterizer).
echo "[2/5] Installing favicon..."
cp "$BRANDING_DIR/favicon.ico" "$STATIC_DIR/assets/favicon.ico"
cp "$BRANDING_DIR/favicon.ico" "$STATIC_DIR/favicon.ico"
cp "$BRANDING_DIR/favicon.svg" "$STATIC_DIR/favicon.svg"
echo "  Installed favicon.ico + favicon.svg"

# --- 3. Patch index.html (title + icon link + loading text) ---
echo "[3/5] Patching index.html..."
if [ -f "$STATIC_DIR/index.html" ]; then
    # Rename the title and inject icon links (the upstream head has none).
    sed -i 's#<title>ComfyUI</title>#<title>Hanzo Studio</title><link rel="icon" type="image/svg+xml" href="favicon.svg"/><link rel="alternate icon" href="favicon.ico"/>#g' "$STATIC_DIR/index.html"
    sed -i 's/Loading ComfyUI/Loading Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/content="ComfyUI/content="Hanzo Studio/g' "$STATIC_DIR/index.html"
    echo "  Patched index.html"
fi

# --- 4. Patch manifest JSON files ---
echo "[4/5] Patching manifests..."
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/"ComfyUI"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/studio\.org/hanzo.ai/g' {} \;
echo "  Done"

# --- 5. Smart JS/CSS/JSON display-string patching ---
echo "[5/5] Smart JS patching..."
python3 "$BRANDING_DIR/patch_frontend.py" "$STATIC_DIR"

echo "=== Hanzo Studio branding complete ==="
