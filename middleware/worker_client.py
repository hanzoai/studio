"""
Worker client for Hanzo Studio worker mode.

When Studio runs with --worker-mode, this module handles:
1. Registering with the coordinator via heartbeats
2. Exposing /api/worker/execute for receiving prompts
3. Reporting device capabilities (GPU model, VRAM, etc.)
"""
import asyncio
import hmac
import logging
import os
import time as _time
import uuid
from typing import Optional

import aiohttp
from aiohttp import web

import execution
from studio.cli_args import args

logger = logging.getLogger(__name__)

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 30

# Shared secret authenticating worker<->coordinator traffic. These endpoints are
# exempt from user IAM (they carry no user token), so without this a rogue caller
# could register a worker URL to hijack an org's jobs. Sourced from KMS in cloud;
# empty in local/dev (single trust domain) where the check is skipped.
WORKER_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")


def verify_worker_token(request) -> bool:
    """True iff the request carries the coordinator shared secret. FAILS CLOSED: in
    worker mode the secret is MANDATORY (main.py refuses to boot without it, and the
    KMS /v1 migration has emptied that secret before), so an empty/unset token is a
    valid single-trust-domain dev signal ONLY when NOT in worker mode — never a bypass
    that reopens the hidden-run hole on a worker box. Constant-time comparison."""
    if not WORKER_TOKEN:
        return not args.worker_mode
    return hmac.compare_digest(request.headers.get("X-Worker-Token", ""), WORKER_TOKEN)


def reject_untrusted_worker_submit(request, worker_mode: bool):
    """The ONE policy gate for worker-mode /prompt (decomplected: one function, one
    place). In worker mode this process is a render BACKEND, not a front — the only
    accepted submit is a job claimed off the org's gpu-jobs queue and handed in over the
    coordinator seam (POST /v1/worker/execute with X-Worker-Token). A direct /prompt POST
    without a valid token is refused (403), so no render can reach a GPU without appearing
    in the org's visible queue. Returns an aiohttp Response to send, or None to allow.
    Not worker mode → always allowed (returns None)."""
    if worker_mode and not verify_worker_token(request):
        return web.json_response({"error": {
            "type": "worker_mode_forbidden",
            "message": "worker-mode renders run only from the gpu-jobs queue "
                       "(POST /v1/worker/execute); direct /prompt is disabled.",
            "details": "", "extra_info": {}}, "node_errors": {}}, status=403)
    return None


def _worker_headers() -> dict:
    return {"X-Worker-Token": WORKER_TOKEN} if WORKER_TOKEN else {}


def get_device_info() -> dict:
    """Detect local device capabilities for worker registration."""
    try:
        import studio.model_management as mm
        device = mm.get_torch_device()
        device_name = mm.get_torch_device_name(device)
        is_cuda = device.type == "cuda"
        vram_total = 0
        if is_cuda:
            vram_total = mm.get_total_memory(device) // (1024 * 1024 * 1024)  # GB
        return {
            "device": "cuda" if is_cuda else "cpu",
            "gpu_model": device_name if is_cuda else None,
            "vram_gb": vram_total if is_cuda else None,
        }
    except Exception as e:
        logger.warning("Could not detect device info: %s", e)
        return {"device": "cpu", "gpu_model": None, "vram_gb": None}


class WorkerClient:
    """Manages heartbeat registration with the coordinator."""

    def __init__(
        self,
        coordinator_url: str,
        worker_id: str,
        worker_port: int = 8188,
        org_id: str = "default",
    ):
        self.coordinator_url = coordinator_url.rstrip("/")
        self.worker_id = worker_id
        self.worker_port = worker_port
        self.org_id = org_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Cache device info once at init (doesn't change at runtime)
        self._device_info = get_device_info()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    def _build_worker_url(self) -> str:
        """Build the URL that the coordinator can use to reach this worker."""
        # Use WORKER_EXTERNAL_URL if set, otherwise construct from listen addr
        external = os.environ.get("WORKER_EXTERNAL_URL")
        if external:
            return external
        # Default: assume coordinator can reach us on the container/host IP
        hostname = os.environ.get("HOSTNAME", "localhost")
        return f"http://{hostname}:{self.worker_port}"

    async def _send_heartbeat(self) -> bool:
        """Send a single heartbeat to the coordinator."""
        payload = {
            "worker_id": self.worker_id,
            "url": self._build_worker_url(),
            "org_id": self.org_id,
            "status": "ready",
            **self._device_info,
        }

        url = f"{self.coordinator_url}/v1/workers/register"
        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=_worker_headers()) as resp:
                if resp.status == 200:
                    logger.debug("Heartbeat sent to %s", url)
                    return True
                logger.warning("Heartbeat failed: %d %s", resp.status, await resp.text())
        except Exception as e:
            logger.warning("Heartbeat error: %s", e)
        return False

    async def _heartbeat_loop(self):
        """Run heartbeats until stopped."""
        while self._running:
            await self._send_heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def start(self):
        """Start the heartbeat loop."""
        self._running = True
        # Send initial heartbeat immediately
        await self._send_heartbeat()
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "Worker %s registered with coordinator %s",
            self.worker_id, self.coordinator_url,
        )

    async def stop(self):
        """Stop heartbeats and clean up."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Worker %s stopped", self.worker_id)


def add_worker_routes(routes: web.RouteTableDef, prompt_server):
    """
    Add the /v1/worker/execute endpoint that receives prompts from the coordinator
    (push model, for in-cluster reachable workers). server.py also serves it under
    the /api alias. NAT'd workers use the pull model instead (see federation doc).
    """

    @routes.post("/v1/worker/execute")
    async def worker_execute(request):
        """Execute a prompt forwarded by the coordinator."""
        if not verify_worker_token(request):
            return web.json_response({"error": "Unauthorized worker"}, status=401)
        try:
            json_data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        if "prompt" not in json_data:
            return web.json_response({"error": "No prompt provided"}, status=400)

        prompt = json_data["prompt"]
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt must be an object"}, status=400)
        prompt_id = str(json_data.get("prompt_id", uuid.uuid4()))

        partial_execution_targets = json_data.get("partial_execution_targets")

        # Apply node replacements (mirrors coordinator behavior in server.py)
        prompt_server.node_replace_manager.apply_replacements(prompt)

        valid = await execution.validate_prompt(prompt_id, prompt, partial_execution_targets)
        if not valid[0]:
            return web.json_response(
                {"error": valid[1], "node_errors": valid[3]}, status=400
            )

        extra_data = json_data.get("extra_data", {})
        if "client_id" in json_data:
            extra_data["client_id"] = json_data["client_id"]

        outputs_to_execute = valid[2]

        # Extract sensitive data (mirrors coordinator behavior)
        sensitive = {}
        for sensitive_val in execution.SENSITIVE_EXTRA_DATA_KEYS:
            if sensitive_val in extra_data:
                sensitive[sensitive_val] = extra_data.pop(sensitive_val)

        extra_data["create_time"] = int(_time.time() * 1000)
        # `org` scopes outputs on THIS worker's LOCAL disk only — a worker-LOCAL label,
        # never a gallery destination. The durable upload path re-derives the gallery org
        # from the user's IAM token (not this body field), so a forged body `org` can at
        # most mislabel a local temp dir, never write into another tenant's gallery. The
        # renderer binds folder_paths.set_execution_org from extra_data["org_id"].
        org = json_data.get("org") or extra_data.get("org_id")
        if org:
            extra_data["org_id"] = org

        # Render it now. A worker claims one job at a time, so the slot is free;
        # if it is not, the claim was premature and the job goes back to the lane.
        if not prompt_server.renderer.start(
                (prompt_id, prompt, extra_data, outputs_to_execute, sensitive)):
            return web.json_response({"error": "already rendering"}, status=503)

        return web.json_response({
            "prompt_id": prompt_id,
            "node_errors": valid[3],
            "worker_id": os.environ.get("WORKER_ID", "local"),
        })
