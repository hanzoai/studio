"""
Visor API client for GPU VM provisioning.

Visor manages GPU instances on AWS (g4dn, p3, p4d, p5) and can be used to
spin up headless Studio workers on demand. This is a thin async HTTP client.
"""
import logging
import os
import shlex
from typing import Optional

import aiohttp

from middleware.compute_config import GPU_TYPE_MAP

logger = logging.getLogger(__name__)

# Default Visor endpoint — override with VISOR_URL env var
VISOR_URL = os.environ.get("VISOR_URL", "https://visor.hanzo.ai")

# Shared session (without baked-in headers — auth sent per-request)
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        )
    return _session


async def close_session():
    """Close the shared session (call on shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def _auth_headers() -> dict:
    """Build auth headers for Visor API (fresh on every call)."""
    token = os.environ.get("VISOR_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def launch_gpu_worker(
    org_id: str,
    gpu_type: str = "t4",
    coordinator_url: str = "",
    worker_id: str = "",
    region: str = "us-east-1",
) -> Optional[dict]:
    """
    Launch a GPU VM via Visor that runs Studio in worker mode.

    Args:
        org_id: Organization identifier
        gpu_type: GPU tier (t4, v100, a100, h100)
        coordinator_url: URL of the Studio coordinator this worker reports to
        worker_id: Unique worker identifier
        region: AWS region

    Returns:
        Visor machine info dict on success, None on failure.
    """
    instance_type = GPU_TYPE_MAP.get(gpu_type)
    if not instance_type:
        logger.error("Unknown gpu_type: %s", gpu_type)
        return None

    payload = {
        "instance_type": instance_type,
        "region": region,
        "tags": {
            "org_id": org_id,
            "worker_id": worker_id,
            "role": "studio-worker",
        },
        "startup_script": _build_startup_script(coordinator_url, worker_id, org_id),
    }

    url = f"{VISOR_URL}/api/launch-machine"
    logger.info("Requesting GPU VM: %s (%s) for org %s", gpu_type, instance_type, org_id)

    try:
        session = await _get_session()
        async with session.post(url, json=payload, headers=_auth_headers()) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                logger.info("GPU VM launched: %s", data.get("machine_id", "unknown"))
                return data
            body = await resp.text()
            logger.error("Visor launch failed (%d): %s", resp.status, body)
    except Exception as e:
        logger.error("Visor launch error: %s", e)

    return None


async def terminate_gpu_worker(machine_id: str) -> bool:
    """Terminate a GPU VM via Visor."""
    url = f"{VISOR_URL}/api/delete-machine"
    payload = {"machine_id": machine_id}

    try:
        session = await _get_session()
        async with session.post(url, json=payload, headers=_auth_headers()) as resp:
            if resp.status == 200:
                logger.info("GPU VM terminated: %s", machine_id)
                return True
            body = await resp.text()
            logger.error("Visor terminate failed (%d): %s", resp.status, body)
    except Exception as e:
        logger.error("Visor terminate error: %s", e)

    return False


async def get_machine_status(machine_id: str) -> Optional[dict]:
    """Check status of a Visor-managed VM."""
    url = f"{VISOR_URL}/api/get-machine"
    params = {"machine_id": machine_id}

    try:
        session = await _get_session()
        async with session.get(url, params=params, headers=_auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.error("Visor status check error: %s", e)

    return None


def _build_startup_script(coordinator_url: str, worker_id: str, org_id: str) -> str:
    """Generate cloud-init/startup script that runs Studio in worker mode.

    All interpolated values are shell-escaped to prevent injection.
    """
    safe_coordinator = shlex.quote(coordinator_url)
    safe_worker_id = shlex.quote(worker_id)
    safe_org_id = shlex.quote(org_id)

    return f"""#!/bin/bash
set -e

# Pull latest studio image
docker pull ghcr.io/hanzoai/studio:latest

# Run studio in worker mode
docker run -d --gpus all \\
    --name studio-worker \\
    -p 8188:8188 \\
    -e WORKER_ID={safe_worker_id} \\
    -e WORKER_EXTERNAL_URL="http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8188" \\
    -e STUDIO_ORG_ID={safe_org_id} \\
    ghcr.io/hanzoai/studio:latest \\
    python main.py \\
        --listen 0.0.0.0 \\
        --worker-mode \\
        --coordinator-url {safe_coordinator} \\
        --worker-id {safe_worker_id}
"""
