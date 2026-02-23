"""
Prompt routing for Hanzo Studio.

Routes prompts to local execution or remote GPU workers based on the org's
compute profile and the prompt's device_preference field.
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from middleware.compute_config import (
    WorkerInfo,
    get_available_gpu_worker,
    load_config,
)

logger = logging.getLogger(__name__)

# How long to wait for a worker to accept a prompt
WORKER_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Shared session for forwarding prompts to workers
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=WORKER_TIMEOUT)
    return _session


async def close_session():
    """Close the shared session (call on shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def route_prompt(
    org_id: str,
    json_data: dict,
) -> Optional[dict]:
    """
    Decide where to execute a prompt.

    Returns:
        None — execute locally (current behavior)
        {"action": "forward", "worker": WorkerInfo, "response": dict} — forwarded to worker
        {"action": "provisioning"} — GPU worker being spun up, caller returns 202
        {"action": "unavailable"} — GPU requested but no worker and no auto-provision
    """
    device_pref = json_data.get("device_preference", "auto")

    # If explicitly CPU or auto, fall through to local
    if device_pref == "cpu":
        return None

    config = load_config(org_id)

    # No GPU enabled in profile — execute locally
    if not config.gpu_enabled and device_pref == "auto":
        return None

    # GPU explicitly requested or profile has GPU enabled
    if device_pref in ("gpu", "cuda") or (device_pref == "auto" and config.gpu_enabled):
        worker = get_available_gpu_worker(org_id)
        if worker:
            result = await forward_to_worker(worker, json_data)
            if result is not None:
                return {"action": "forward", "worker": worker, "response": result}
            # Worker failed — fall through to local
            logger.warning("Worker %s failed, falling through to local", worker.worker_id)

        # No available worker
        if config.auto_provision:
            return {"action": "provisioning"}

        if device_pref in ("gpu", "cuda"):
            return {"action": "unavailable"}

    # Default: execute locally
    return None


async def forward_to_worker(worker: WorkerInfo, json_data: dict) -> Optional[dict]:
    """
    Forward a prompt to a remote worker via HTTP POST.

    The worker exposes /api/worker/execute which accepts the same JSON body
    as POST /prompt and returns the same response shape.

    Returns the worker's JSON response or None on failure.
    """
    url = f"{worker.url.rstrip('/')}/api/worker/execute"
    logger.info("Forwarding prompt to worker %s at %s", worker.worker_id, url)

    try:
        session = await _get_session()
        async with session.post(url, json=json_data) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning(
                "Worker %s returned status %d: %s",
                worker.worker_id, resp.status, await resp.text()
            )
    except asyncio.TimeoutError:
        logger.warning("Timeout forwarding to worker %s", worker.worker_id)
    except Exception as e:
        logger.warning("Error forwarding to worker %s: %s", worker.worker_id, e)

    return None


async def check_worker_health(worker: WorkerInfo) -> bool:
    """Ping a worker's health endpoint."""
    url = f"{worker.url.rstrip('/')}/health"
    try:
        session = await _get_session()
        async with session.get(url) as resp:
            return resp.status == 200
    except Exception:
        return False
