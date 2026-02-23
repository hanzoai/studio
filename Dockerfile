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

# Copy application code
COPY . .

# Create directories for runtime volumes
RUN mkdir -p models output input custom_nodes user

EXPOSE 8188

CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu"]
