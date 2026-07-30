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
#   STUDIO_BILLING_CHECK_BALANCE=1 - Check balance before prompts
#   STUDIO_COMMERCE_URL=http://commerce.hanzo.svc:8001
#   STUDIO_COMMERCE_TOKEN=...     - Commerce API token
#   STUDIO_ENABLE_METRICS=1       - /metrics endpoint for VictoriaMetrics
#   STUDIO_RATE_LIMIT_RPM=60      - Prompts per minute per org
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu"]
