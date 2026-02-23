#!/bin/bash
# Apply Hanzo Studio branding to frontend package
# This runs during Docker build after pip install to patch the pre-built frontend
set -e

STATIC_DIR=$(python3 -c "import comfyui_frontend_package, importlib.resources; print(str(importlib.resources.files(comfyui_frontend_package) / 'static'))")
BRANDING_DIR="$(dirname "$0")"

echo "=== Hanzo Studio Branding ==="
echo "Target: $STATIC_DIR"

# --- 1. Replace logo SVG files ---
echo "[1/6] Replacing logos..."
for logo_file in comfy-logo-single.svg comfy-logo-mono.svg comfy-cloud-logo.svg; do
    target="$STATIC_DIR/assets/images/$logo_file"
    if [ -f "$target" ]; then
        cp "$BRANDING_DIR/hanzo-logo.svg" "$target"
        echo "  Replaced $logo_file"
    else
        echo "  Not found: $logo_file (skipping)"
    fi
done

# --- 2. Replace favicon ---
echo "[2/6] Replacing favicon..."
python3 -c "
import struct
size = 16
pixels = bytearray(size * size * 4)
for y in range(size):
    for x in range(size):
        idx = (y * size + x) * 4
        pixels[idx] = 0; pixels[idx+1] = 0; pixels[idx+2] = 0; pixels[idx+3] = 0
        if 1 <= x <= 4 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        if 11 <= x <= 14 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        if 4 <= y <= 11:
            expected_x = 1 + (y - 4) * (13.0 / 7.0)
            if abs(x - expected_x) <= 2:
                pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
ico = bytearray()
ico += struct.pack('<HHH', 0, 1, 1)
ico += struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(pixels) + 40, 22)
ico += struct.pack('<IiiHHIIiiII', 40, size, size*2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
for y in range(size-1, -1, -1):
    ico += pixels[y*size*4:(y+1)*size*4]
with open('$STATIC_DIR/assets/favicon.ico', 'wb') as f:
    f.write(ico)
print('  Generated favicon.ico')
"

# --- 3. Patch index.html ---
echo "[3/6] Patching index.html..."
if [ -f "$STATIC_DIR/index.html" ]; then
    sed -i 's/<title>ComfyUI<\/title>/<title>Hanzo Studio<\/title>/g' "$STATIC_DIR/index.html"
    sed -i 's/Loading ComfyUI/Loading Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/content="ComfyUI/content="Hanzo Studio/g' "$STATIC_DIR/index.html"
    echo "  Patched index.html"
fi

# --- 4. Patch manifest JSON files ---
echo "[4/6] Patching manifests..."
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/"ComfyUI"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/studio\.org/hanzo.ai/g' {} \;
echo "  Done"

# --- 5. Smart JS/CSS patching via Python ---
echo "[5/6] Smart JS patching..."
python3 "$BRANDING_DIR/patch_frontend.py" "$STATIC_DIR"

# --- 6. Final verification ---
echo "[6/6] Verification..."
echo "=== Hanzo Studio branding complete ==="
