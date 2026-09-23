# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        # WeasyPrint native stack (office-document PDF renderer, middleware/doc_render.py):
        # pango/cairo/gdk-pixbuf + a base font. Absent → the built-in PDF fallback is used.
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Rendering a film (middleware/film.py): the route shells out to the @hanzo/frames
# CLI, which drives a headless browser and muxes with ffmpeg, and refuses 503
# naming whichever of npx/node/ffmpeg/ffprobe it cannot find. The CLI version is
# pinned in film.py, so it is named once and not here. `chromium` is installed
# for the shared libraries a browser links against, which apt resolves as its
# own dependencies, and `unzip` because the browser installer below extracts a
# zip and treats a missing archiver as every download provider failing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates unzip ffmpeg chromium \
        fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/* && apt-get clean && fc-cache -f

# Node comes from the official image rather than a distribution package: the CLI
# refuses a runtime below 22 (`MINIMUM_NODE_MAJOR`) and Debian 13 carries 20.
# The tag is the version, so a bump is one word here. trixie matches this base,
# so the binary links against the glibc already present.
COPY --from=node:22-trixie-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-trixie-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npx --version

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# chrome-headless-shell captures by BeginFrame, which is what makes a frame the
# same every render; plain chromium seeks by wall clock and stutters. linux64
# only — the one platform studio builds and runs (deploy.yml `platforms:
# linux/amd64`), and the only one chrome-for-testing publishes. The installer
# nests the binary under a version directory it chooses, so the build links it
# to one stable name and the environment below states that name, which is how a
# call site never passes a browser path.
RUN set -eu; \
    npx --yes @puppeteer/browsers install chrome-headless-shell@stable --path /opt/chrome; \
    bin="$(find /opt/chrome -type f -name chrome-headless-shell | head -1)"; \
    [ -n "$bin" ] || { echo "chrome-headless-shell did not land under /opt/chrome" >&2; exit 1; }; \
    ln -s "$bin" /usr/local/bin/chrome-headless-shell; \
    /usr/local/bin/chrome-headless-shell --version

ENV PRODUCER_HEADLESS_SHELL_PATH=/usr/local/bin/chrome-headless-shell

WORKDIR /app

# Install PyTorch CPU first (large layer, cached separately).
# The pip cache is a BuildKit CACHE MOUNT, not a layer: --no-cache-dir made every
# build re-download the whole torch stack over the network (the fabric passes no
# --import-cache either, so nothing was reused). The mount is not part of the image,
# so the image stays exactly as small as before.
RUN --mount=type=cache,target=/root/.cache/pip pip install \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Apply Hanzo Studio branding to frontend package
COPY branding/ /tmp/branding/
RUN chmod +x /tmp/branding/apply-branding.sh && /tmp/branding/apply-branding.sh && rm -rf /tmp/branding

# Copy application code
COPY . .

# The renderer, installed at the version film.py names — read from the source
# rather than repeated, so the pin has one home and a bump is one edit there.
# Installed rather than left to `npx --yes`, which reaches npm on every render.
RUN set -eu; \
    pin="$(sed -n 's/^RENDERER = "\(.*\)"$/\1/p' middleware/film.py)"; \
    [ -n "$pin" ] || { echo "middleware/film.py names no RENDERER to install" >&2; exit 1; }; \
    npm install -g "$pin"; \
    frames --version

# Vendor the Hanzo Studio custom-node packs (segmentation, controlnet, ipadapter,
# upscaling, video, utils) at their pinned commits and install their deps with the
# torch/numpy/transformers stack locked. See custom_nodes/hanzo-packs.txt.
RUN chmod +x scripts/install_custom_nodes.sh && scripts/install_custom_nodes.sh

# Create directories for runtime volumes
RUN mkdir -p models output input custom_nodes user

# /app/orgs is the tenant-data PVC; /app/models is ephemeral overlay. Register
# .models on the PVC as the default model store (main.py auto-loads this file)
# so models survive pod restarts and downloads land durable.
RUN printf '%s\n' \
    'hanzo:' \
    '  base_path: /app/orgs/.models' \
    '  is_default: true' \
    '  checkpoints: checkpoints' \
    '  configs: configs' \
    '  loras: loras' \
    '  vae: vae' \
    '  text_encoders: text_encoders' \
    '  diffusion_models: diffusion_models' \
    '  clip_vision: clip_vision' \
    '  style_models: style_models' \
    '  embeddings: embeddings' \
    '  diffusers: diffusers' \
    '  vae_approx: vae_approx' \
    '  controlnet: controlnet' \
    '  gligen: gligen' \
    '  upscale_models: upscale_models' \
    '  latent_upscale_models: latent_upscale_models' \
    '  hypernetworks: hypernetworks' \
    '  photomaker: photomaker' \
    '  classifiers: classifiers' \
    '  model_patches: model_patches' \
    '  audio_encoders: audio_encoders' \
    > extra_model_paths.yaml

EXPOSE 8188

# Default: listen on all interfaces, CPU mode
# Environment variables:
#   STUDIO_ENABLE_IAM_AUTH=1      - Enable IAM auth via hanzo.id
#   STUDIO_IAM_URL=https://hanzo.id
#   STUDIO_NO_LOCALHOST_BYPASS=1  - Require auth even for localhost
#   STUDIO_MULTI_TENANT=1         - Per-org storage isolation
#   STUDIO_ORG_ID=default-org     - Default org ID
#   STUDIO_ENABLE_BILLING=1       - Commerce billing integration
#   STUDIO_BILLING_CHECK_BALANCE=1 - Check the org balance before prompts
#   STUDIO_COMMERCE_URL=http://cloud.hanzo.svc:8000 - commerce API base
#   STUDIO_IAM_CLIENT_ID / STUDIO_IAM_CLIENT_SECRET - the IAM app; billing calls
#                                   use its client_credentials token
#   STUDIO_ENABLE_METRICS=1       - /metrics endpoint for VictoriaMetrics
#   STUDIO_RATE_LIMIT_RPM=60      - Prompts per minute per org
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu"]
