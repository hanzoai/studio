#!/bin/bash
# Apply Hanzo Studio branding to the two prebuilt packages the user reads: the
# frontend bundle (comfyui-frontend-package) and the node help docs
# (comfyui-embedded-docs). Runs during the Docker build after pip install, and
# can be run against a venv for local dev. Idempotent: safe to run more than once.
set -e

STATIC_DIR=$(python3 -c "import comfyui_frontend_package, importlib.resources; print(str(importlib.resources.files(comfyui_frontend_package) / 'static'))")
DOCS_DIR=$(python3 -c "import comfyui_embedded_docs, importlib.resources; print(str(importlib.resources.files(comfyui_embedded_docs) / 'docs'))")
BRANDING_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Hanzo Studio Branding ==="
echo "Frontend: $STATIC_DIR"
echo "Node docs: $DOCS_DIR"

# --- 1. Replace in-app logos ---
echo "[1/13] Replacing logos..."
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
# request. During a generation it instead cycles the 10 progress frames under
# assets/images/favicon_progress_16x16/, so those must be Hanzo-branded too or the
# tab reverts to the Comfy mark while a job runs. All PNG/ICO are committed
# artifacts (rendered by make_favicon.py / make_progress_favicons.py) so the Docker
# image needs no rasterizer.
echo "[2/13] Installing favicon..."
cp "$BRANDING_DIR/favicon.ico" "$STATIC_DIR/assets/favicon.ico"
cp "$BRANDING_DIR/favicon.ico" "$STATIC_DIR/favicon.ico"
cp "$BRANDING_DIR/favicon.svg" "$STATIC_DIR/favicon.svg"
progress_dst="$STATIC_DIR/assets/images/favicon_progress_16x16"
if [ -d "$progress_dst" ]; then
    cp "$BRANDING_DIR"/favicon_progress_16x16/frame_*.png "$progress_dst/"
    echo "  Installed favicon.ico + favicon.svg + progress frames"
else
    echo "  Installed favicon.ico + favicon.svg (progress dir absent, skipped)"
fi

# --- 3. Patch index.html (title + icon link + loading text) ---
echo "[3/13] Patching index.html..."
if [ -f "$STATIC_DIR/index.html" ]; then
    # Rename the title and inject icon links (the upstream head has none).
    sed -i 's#<title>ComfyUI</title>#<title>Hanzo Studio</title><link rel="icon" type="image/svg+xml" href="favicon.svg"/><link rel="alternate icon" href="favicon.ico"/>#g' "$STATIC_DIR/index.html"
    sed -i 's/Loading ComfyUI/Loading Hanzo Studio/g' "$STATIC_DIR/index.html"
    sed -i 's/content="ComfyUI/content="Hanzo Studio/g' "$STATIC_DIR/index.html"
    echo "  Patched index.html"
fi

# --- 4. Patch manifest JSON files ---
echo "[4/13] Patching manifests..."
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/"ComfyUI"/"Hanzo Studio"/g' {} \;
find "$STATIC_DIR" -name "manifest*.json" -exec sed -i 's/studio\.org/hanzo.ai/g' {} \;
echo "  Done"

# --- 5. Smart JS/CSS/JSON display-string patching ---
echo "[5/13] Smart JS patching..."
python3 "$BRANDING_DIR/patch_frontend.py" "$STATIC_DIR"

# --- 6. Replace the inline sidebar logo (ComfyLogo Vue component) ---
# This is the top-left in-app mark, an inline SVG compiled into a JS chunk — a
# different asset from the file-based logos handled in step 1.
echo "[6/13] Replacing in-app sidebar logo..."
python3 "$BRANDING_DIR/patch_sidebar_logo.py" "$STATIC_DIR"

# --- 7. Apply the black + Hanzo-purple theme ---
# Installs hanzo-theme.css, injects it as the last stylesheet in index.html, and
# rewrites the "dark" palette's canvas clear color to pure black.
echo "[7/13] Applying black + purple theme..."
python3 "$BRANDING_DIR/patch_theme.py" "$STATIC_DIR" "$BRANDING_DIR"

# --- 8. Seed the default 25-tab fashion workspace ---
# Injects a <head> bootstrap that pre-populates the localStorage keys the
# frontend reads to restore open workflow tabs. Idempotent; bump SEED_VERSION
# in patch_workspace_seed.py to force existing profiles to re-seed once.
echo "[8/13] Seeding default workspace tabs..."
python3 "$BRANDING_DIR/patch_workspace_seed.py" "$STATIC_DIR"

# --- 9. Inject the Copilot sidebar chat (frontend bundle + on-brand CSS) ---
# Copies hanzo-copilot.{js,css} into static/ and injects them into index.html
# (CSS link in <head>, deferred <script> before </body>). Pairs with the backend
# route POST /v1/copilot/chat. Idempotent; bump BUNDLE_VERSION in patch_copilot.py.
echo "[9/13] Injecting Copilot..."
python3 "$BRANDING_DIR/patch_copilot.py" "$STATIC_DIR" "$BRANDING_DIR"

# --- 10. Inject the Identity user menu (frontend bundle + on-brand CSS) ---
# Top-right user pill driven by GET /v1/session: login/logout, org switcher,
# project scope, Console link. Idempotent; bump BUNDLE_VERSION in patch_identity.py.
echo "[10/13] Injecting Identity menu..."
python3 "$BRANDING_DIR/patch_identity.py" "$STATIC_DIR" "$BRANDING_DIR"

# --- 11. De-Comfy remaining display strings, every language ---
# Sweeps both trees the user reads for user-visible 'ComfyUI'/'Comfy' -> Studio
# that the curated phrase list in patch_frontend.py cannot reach (mid-string),
# plus the Settings dialog section headers. The node docs ship as their own
# package, so they need the same sweep pointed at them. Runs after the frontend
# passes so it mops up everything.
echo "[11/13] De-Comfy display strings..."
python3 "$BRANDING_DIR/patch_destring.py" "$STATIC_DIR"
python3 "$BRANDING_DIR/patch_destring.py" "$DOCS_DIR"

# --- 12. Force "Nodes 2.0" (Comfy.VueNodes.Enabled) on, remove the toggles ---
# studio.hanzo.ai ships the modern DOM node renderer as the only node design:
# default the setting on, hide it from the Settings dialog (type:hidden), and
# drop the logo-menu switch. Idempotent; see patch_vuenodes.py.
echo "[12/13] Forcing Nodes 2.0 on (no toggle)..."
python3 "$BRANDING_DIR/patch_vuenodes.py" "$STATIC_DIR"

# --- 13. Inject the deep-link opener (open + optionally run a workflow by URL) ---
# ?org=<org>&workflow=<path>&run=1 sets the session org, loads the saved workflow,
# and queues it (dispatching to the org's BYO-GPU). Idempotent; see patch_deeplink.py.
echo "[13/13] Injecting deep-link opener..."
python3 "$BRANDING_DIR/patch_deeplink.py" "$STATIC_DIR" "$BRANDING_DIR"

echo "=== Hanzo Studio branding complete ==="
