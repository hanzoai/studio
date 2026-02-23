FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch CPU first (large layer, cached separately)
RUN pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Apply Hanzo Studio branding to frontend package
COPY branding/ /tmp/branding/
RUN chmod +x /tmp/branding/apply-branding.sh && /tmp/branding/apply-branding.sh && rm -rf /tmp/branding

# Copy application code
COPY . .

# Create directories for runtime volumes
RUN mkdir -p models output input custom_nodes user

EXPOSE 8188

# Default: listen on all interfaces, CPU mode
# IAM auth and multi-tenant are configured via environment variables:
#   STUDIO_IAM_URL=https://hanzo.id
#   STUDIO_ENABLE_IAM_AUTH=1
#   STUDIO_MULTI_TENANT=1
#   STUDIO_ORG_ID=default-org
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu"]
