#!/bin/bash
# Apply Hanzo Studio branding to ComfyUI frontend package
# This runs during Docker build after pip install to patch the pre-built frontend
set -e

STATIC_DIR=$(python3 -c "import comfyui_frontend_package, importlib.resources; print(str(importlib.resources.files(comfyui_frontend_package) / 'static'))")
BRANDING_DIR="$(dirname "$0")"

echo "=== Hanzo Studio Branding ==="
echo "Target: $STATIC_DIR"

# --- 1. Replace logo SVG files ---
echo "[1/7] Replacing logos..."
for logo_file in comfy-logo-single.svg comfy-logo-mono.svg comfy-cloud-logo.svg; do
    target="$STATIC_DIR/assets/images/$logo_file"
    if [ -f "$target" ]; then
        cp "$BRANDING_DIR/hanzo-logo.svg" "$target"
        echo "  Replaced $logo_file"
    fi
done

# Also replace any other logo SVGs we find
find "$STATIC_DIR" -name "*comfy*logo*" -o -name "*logo*comfy*" 2>/dev/null | while read f; do
    if [[ "$f" == *.svg ]]; then
        cp "$BRANDING_DIR/hanzo-logo.svg" "$f"
        echo "  Replaced $(basename "$f")"
    fi
done

# --- 2. Replace favicon ---
echo "[2/7] Replacing favicon..."
python3 -c "
import struct

size = 16
pixels = bytearray(size * size * 4)

for y in range(size):
    for x in range(size):
        idx = (y * size + x) * 4
        pixels[idx] = 0; pixels[idx+1] = 0; pixels[idx+2] = 0; pixels[idx+3] = 0
        # Left vertical bar
        if 1 <= x <= 4 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        # Right vertical bar
        if 11 <= x <= 14 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        # Diagonal bar
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

# Also replace any other favicon files
find "$STATIC_DIR" -name "favicon*" ! -name "favicon.ico" 2>/dev/null | while read f; do
    if [[ "$f" == *.svg ]]; then
        cp "$BRANDING_DIR/favicon.svg" "$f"
        echo "  Replaced $(basename "$f")"
    fi
done

# --- 3. Patch index.html ---
echo "[3/7] Patching index.html..."
if [ -f "$STATIC_DIR/index.html" ]; then
    sed -i 's/<title>ComfyUI<\/title>/<title>Hanzo Studio<\/title>/g' "$STATIC_DIR/index.html"
    sed -i 's/<title>Comfy<\/title>/<title>Hanzo Studio<\/title>/g' "$STATIC_DIR/index.html"
    sed -i 's/Loading ComfyUI/Loading Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/Loading Comfy/Loading Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/content="ComfyUI/content="Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/content="Comfy/content="Hanzo Studio/g' "$STATIC_DIR/index.html"
    echo "  Patched index.html"
fi

# --- 4. Patch manifest JSON files ---
echo "[4/7] Patching manifests..."
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/"ComfyUI"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/"Comfy"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/ComfyUI: AI image generation platform/Hanzo Studio: Visual AI Engine/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/comfy\.org/hanzo.ai/g' {} \;
echo "  Done"

# --- 5. Comprehensive JS bundle patching ---
echo "[5/7] Patching ALL JS bundles..."
JS_COUNT=0
for js in "$STATIC_DIR"/assets/*.js "$STATIC_DIR"/*.js; do
    [ -f "$js" ] || continue

    # Brand names
    sed -i 's/ComfyUI/Hanzo Studio/g' "$js"

    # Organization names
    sed -i 's/ComfyOrg/Hanzo AI/g' "$js"
    sed -i 's/Comfy-Org/Hanzo AI/g' "$js"
    sed -i 's/Comfy Org/Hanzo AI/g' "$js"

    # Domain replacements
    sed -i 's/comfy\.org/hanzo.ai/g' "$js"
    sed -i 's/docs\.comfy\.org/docs.hanzo.ai/g' "$js"
    sed -i 's/forum\.comfy\.org/hanzo.ai\/community/g' "$js"
    sed -i 's/api\.comfy\.org/api.hanzo.ai/g' "$js"
    sed -i 's/platform\.comfy\.org/hanzo.ai/g' "$js"

    # GitHub URLs
    sed -i 's|github\.com/comfyanonymous/ComfyUI|github.com/hanzoai/studio|g' "$js"
    sed -i 's|github\.com/Comfy-Org/ComfyUI|github.com/hanzoai/studio|g' "$js"
    sed -i 's|github\.com/Comfy-Org|github.com/hanzoai|g' "$js"
    sed -i 's|github\.com/comfyanonymous|github.com/hanzoai|g' "$js"

    # Discord - replace Comfy discord with Hanzo discord
    sed -i 's|discord\.gg/comfyorg|discord.gg/hanzoai|g' "$js"
    sed -i 's|discord\.com/invite/comfyorg|discord.com/invite/hanzoai|g' "$js"
    # Catch other Comfy discord patterns
    sed -i 's|discord\.gg/[a-zA-Z0-9]*comfy[a-zA-Z0-9]*|discord.gg/hanzoai|g' "$js"

    # Support email
    sed -i 's/support@comfy\.org/support@hanzo.ai/g' "$js"

    # Clean up any double-replacements (e.g., "Hanzo Studio Studio")
    sed -i 's/Hanzo Studio Studio/Hanzo Studio/g' "$js"
    sed -i 's/Hanzo Studio Manager/Hanzo Studio Manager/g' "$js"

    JS_COUNT=$((JS_COUNT + 1))
done
echo "  Patched $JS_COUNT JS files"

# --- 6. Patch CSS files ---
echo "[6/7] Patching CSS..."
for css in "$STATIC_DIR"/assets/*.css "$STATIC_DIR"/*.css; do
    [ -f "$css" ] || continue
    # Remove any ComfyUI-specific animation classes that reference the C logo
    # (The spinning C logo animation)
    sed -i 's/comfy\.org/hanzo.ai/g' "$css"
done
echo "  Done"

# --- 7. Final verification ---
echo "[7/7] Verification..."
REMAINING=$(grep -rl "ComfyUI" "$STATIC_DIR" --include="*.html" --include="*.js" --include="*.json" 2>/dev/null | wc -l | tr -d ' ')
echo "  Files still containing 'ComfyUI': $REMAINING"
if [ "$REMAINING" -gt 0 ]; then
    echo "  Remaining references (for review):"
    grep -rl "ComfyUI" "$STATIC_DIR" --include="*.html" --include="*.js" --include="*.json" 2>/dev/null | head -5
fi

echo ""
echo "=== Hanzo Studio branding complete ==="
