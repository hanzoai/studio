"""
Engine selector for Hanzo Studio.

Lists the execution targets ("engines") available to an org and records the
org's default. Thin wrapper over the existing per-org compute registry
(middleware/compute_config.py): an engine is either

  - `local`   — this Studio instance, always present, and
  - a registered org GPU worker (from compute.json's worker registry).

Selecting an engine names which target a prompt without an explicit
device_preference lands on; gpu_dispatch carries it there.

Leased cloud machines (via the platform's Visor compute surface,
`GET {cloud}/v1/machines`) are a future engine class — the client interface is
noted below but not built this iteration; the API shape here already
accommodates the extra entries.
"""
from __future__ import annotations

import logging

from aiohttp import web

from middleware import compute_config

logger = logging.getLogger(__name__)


# Future: leased-machine engines. A VisorMachinesClient would implement
#   list_machines(org_id) -> list[dict]  hitting GET {STUDIO_CLOUD_API_URL}/v1/machines
# and its rows would be appended to list_engines() as {"type": "leased", ...}.
# Console wiring + the client land in a later iteration; not built now.


def list_engines(org_id: str) -> dict:
    """Available engines for org_id plus the current default."""
    profile = compute_config.load_config(org_id)
    engines = [{
        "id": "local",
        "type": "local",
        "name": "This instance",
        "status": "ready",
        "device": "local",
    }]
    for w in profile.workers:
        engines.append({
            "id": w.worker_id,
            "type": "worker",
            "name": w.gpu_model or w.worker_id,
            "status": "ready" if (w.status == "ready" and w.is_alive()) else "offline",
            "device": w.device,
            "vram_gb": w.vram_gb,
            "url": w.url,
        })
    default = profile.default_engine or "local"
    if default != "local" and not any(e["id"] == default for e in engines):
        # The default engine deregistered — fall back to local, don't 500.
        default = "local"
    return {"engines": engines, "default": default}


def resolve_engine_worker(org_id: str):
    """If the org's default engine is a live worker, return it; else None (local)."""
    profile = compute_config.load_config(org_id)
    default = profile.default_engine or "local"
    if default == "local":
        return None
    for w in profile.workers:
        if w.worker_id == default and w.is_alive():
            return w
    return None


def add_engine_routes(routes: web.RouteTableDef, server):
    """Register /v1/engines on the coordinator."""

    @routes.get("/v1/engines")
    async def get_engines(request):
        org_id = server._get_org_id(request)
        return web.json_response(list_engines(org_id))

    @routes.put("/v1/engines/default")
    async def set_default_engine(request):
        org_id = server._get_org_id(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        engine_id = body.get("engine")
        if not engine_id:
            return web.json_response({"error": "engine is required"}, status=400)
        valid = {e["id"] for e in list_engines(org_id)["engines"]}
        if engine_id not in valid:
            return web.json_response(
                {"error": f"unknown engine {engine_id!r}", "engines": sorted(valid)}, status=400
            )
        compute_config.update_config(org_id, {"default_engine": engine_id})
        return web.json_response(list_engines(org_id))
