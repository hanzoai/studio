#!/bin/bash
# Apply Hanzo Studio branding to ComfyUI frontend
set -e

STATIC_DIR=$(python3 -c "import comfyui_frontend_package, importlib.resources; print(str(importlib.resources.files(comfyui_frontend_package) / 'static'))")
BRANDING_DIR="$(dirname "$0")"

echo "Applying Hanzo Studio branding to: $STATIC_DIR"

# 1. Replace logo SVGs
cp "$BRANDING_DIR/hanzo-logo.svg" "$STATIC_DIR/assets/images/comfy-logo-single.svg"
cp "$BRANDING_DIR/hanzo-logo.svg" "$STATIC_DIR/assets/images/comfy-logo-mono.svg"
cp "$BRANDING_DIR/hanzo-logo.svg" "$STATIC_DIR/assets/images/comfy-cloud-logo.svg"

# 2. Replace favicon - convert SVG to ICO using Python
python3 -c "
import struct, io, base64, xml.etree.ElementTree as ET

# Read the favicon SVG
with open('$BRANDING_DIR/favicon.svg', 'r') as f:
    svg_content = f.read()

# Create a minimal 16x16 ICO with the H mark
# Use a simple 16x16 bitmap representation
size = 16
# Simple H pattern at 16x16
pixels = bytearray(size * size * 4)  # BGRA

# Draw the H mark (simplified geometric pattern)
for y in range(size):
    for x in range(size):
        idx = (y * size + x) * 4
        # Background: transparent
        pixels[idx] = 0     # B
        pixels[idx+1] = 0   # G
        pixels[idx+2] = 0   # R
        pixels[idx+3] = 0   # A

        # Left vertical bar (0-4, 0-15)
        if 1 <= x <= 4 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        # Right vertical bar (11-14, 0-15)
        if 11 <= x <= 14 and 1 <= y <= 14:
            pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255
        # Diagonal bar (connecting, roughly y=4 to y=11, x=1 to x=14)
        if 4 <= y <= 11:
            expected_x = 1 + (y - 4) * (13.0 / 7.0)
            if abs(x - expected_x) <= 2:
                pixels[idx] = 255; pixels[idx+1] = 255; pixels[idx+2] = 255; pixels[idx+3] = 255

# ICO format
ico = bytearray()
# ICONDIR
ico += struct.pack('<HHH', 0, 1, 1)  # reserved, type=1(icon), count=1
# ICONDIRENTRY
ico += struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(pixels) + 40, 22)
# BITMAPINFOHEADER
ico += struct.pack('<IiiHHIIiiII', 40, size, size*2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
# Pixel data (bottom-up)
for y in range(size-1, -1, -1):
    ico += pixels[y*size*4:(y+1)*size*4]

with open('$STATIC_DIR/assets/favicon.ico', 'wb') as f:
    f.write(ico)
print('Generated favicon.ico')
"

# 3. Patch index.html
sed -i 's/<title>ComfyUI<\/title>/<title>Hanzo Studio<\/title>/g' "$STATIC_DIR/index.html"
sed -i 's/Loading ComfyUI\.\.\./Loading Hanzo Studio.../g' "$STATIC_DIR/index.html"

# 4. Patch manifest JSON
find "$STATIC_DIR/assets" -name "manifest-*.json" -exec sed -i 's/"ComfyUI"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR/assets" -name "manifest-*.json" -exec sed -i 's/ComfyUI: AI image generation platform/Hanzo Studio: Visual AI Engine/g' {} \;

# 5. Patch visible "ComfyUI" strings in JS bundles (user-facing only)
# Target the main bundle and commands for UI-visible strings
for js in "$STATIC_DIR/assets"/main-*.js "$STATIC_DIR/assets"/commands-*.js "$STATIC_DIR/assets"/settings-*.js; do
    [ -f "$js" ] || continue
    # Replace user-visible ComfyUI references
    sed -i 's/ComfyUI/Hanzo Studio/g' "$js"
done

# 6. Patch the AboutPanel
for js in "$STATIC_DIR/assets"/AboutPanel-*.js; do
    [ -f "$js" ] || continue
    sed -i 's/ComfyUI/Hanzo Studio/g' "$js"
    sed -i 's/comfy\.org/hanzo.ai/g' "$js"
    sed -i 's/ComfyOrg/Hanzo AI/g' "$js"
done

# 7. Patch ComfyOrgHeader
for js in "$STATIC_DIR/assets"/ComfyOrgHeader-*.js; do
    [ -f "$js" ] || continue
    sed -i 's/ComfyOrg/Hanzo AI/g' "$js"
    sed -i 's/comfy\.org/hanzo.ai/g' "$js"
done

echo "Hanzo Studio branding applied successfully"
