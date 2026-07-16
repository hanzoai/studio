"""Studio Home — the content-forward default view (flow-style), with the node
editor demoted to "Advanced mode".

The normal user works per DESIGN, not per node graph: see the assets a pipeline
produced, judge them (status chips make blotchy rejects vs approved picks
obvious), re-run or chat-fix a given output, and step into the graph editor only
when they mean to. This module adds that surface without inventing any new
storage or lifecycle:

    GET  /studio                 the home page (auth-gated like everything else)
    GET  /v1/library             org's library.json + per-asset workflow provenance
    POST /v1/library/status      {path, status}  draft|approved|queued|published
    POST /v1/library/rerun       {path, count?}  re-queue the EXACT embedded graph, new seed
    POST /v1/library/fix         {path, instruction}  chat-fix: Qwen edit pass on that output

One source of truth per concern, all pre-existing:
  * assets + curation state  -> orgs/<org>/output/library.json (library_manifest.py)
  * what made an image       -> the API graph ComfyUI embeds in the PNG itself
  * running work             -> the ONE /prompt path (self-POST, so gpu_dispatch,
                                billing and content_publish all apply unchanged)
  * identity                 -> the caller's own session; headers are forwarded
                                verbatim on the self-POST, no token is stored
  * workflow editing         -> the 0.15.1 deep-link opener (?workflow=&run=1)

Env:
    STUDIO_HOME_DEFAULT  "1"/"true": browser GET / redirects to /studio
                         (the SPA stays reachable at /?advanced=1 and all
                         non-root paths). Off by default so local dev and
                         worker pods are untouched.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import folder_paths

log = logging.getLogger("studio.home")

_HTML = Path(__file__).with_name("studio_home.html")
_STATUSES = ("draft", "approved", "queued", "published", "deleted")
_PROV_CACHE: dict[str, tuple[float, dict]] = {}  # abspath -> (mtime, provenance)

_NEG = ("ghosting, double exposure, duplicated body, transparent fabric, sheer "
        "fabric, illustration, painterly, blotchy skin, mottled skin, jpeg "
        "artifacts, pixelation, noise, deformed hands, extra limbs, text, watermark")


def _home_default() -> bool:
    return os.environ.get("STUDIO_HOME_DEFAULT", "").lower() in ("1", "true", "yes")


def _org_of(request: web.Request) -> str:
    user = request.get("iam_user") or {}
    return user.get("org_id") or os.environ.get("STUDIO_ORG_ID") or "default"


def _library_root(org: str) -> Path | None:
    """The org's asset tree. Pod layout first (orgs/<org>/output via
    folder_paths), then the flat-dev staging layout (output/orgs/<org>/output)."""
    for cand in (
        Path(folder_paths.get_org_output_directory(org)),
        Path(folder_paths.get_output_directory()) / "orgs" / org / "output",
    ):
        if (cand / "library.json").is_file():
            return cand
    return None


def _view_params(abs_path: Path) -> dict:
    """Query params for the existing org-scoped /view endpoint."""
    out = Path(folder_paths.get_output_directory())
    try:
        rel = abs_path.relative_to(out)
    except ValueError:
        # org-scoped dir outside the flat output dir (pod layout): /view resolves
        # the org dir itself in multi-tenant mode, so make the path org-relative.
        for org_base in abs_path.parents:
            if org_base.name == "output" and org_base.parent.parent.name == "orgs":
                rel = abs_path.relative_to(org_base)
                break
        else:
            rel = Path(abs_path.name)
    return {"filename": rel.name, "subfolder": str(rel.parent) if str(rel.parent) != "." else "", "type": "output"}


def _provenance(abs_path: Path) -> dict:
    """What made this image, from the graph ComfyUI embeds in the PNG."""
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        return {}
    key = str(abs_path)
    hit = _PROV_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    prov: dict = {}
    if abs_path.suffix.lower() == ".png":
        try:
            from PIL import Image
            info = Image.open(abs_path).info
            graph = json.loads(info.get("prompt", "null"))
            if isinstance(graph, dict):
                for node in graph.values():
                    ct = node.get("class_type", "")
                    ins = node.get("inputs", {})
                    if ct == "SaveImage":
                        prov["workflow"] = ins.get("filename_prefix", "")
                    elif "KSampler" in ct:
                        prov.update(steps=ins.get("steps"), cfg=ins.get("cfg"),
                                    seed=ins.get("seed", ins.get("noise_seed")))
                prov["nodes"] = len(graph)
        except Exception:  # non-Comfy PNG, truncated file — provenance is optional
            prov = {}
    _PROV_CACHE[key] = (mtime, prov)
    return prov


def _load_library(root: Path) -> dict:
    try:
        return json.loads((root / "library.json").read_text())
    except Exception:
        return {"assets": []}


def _save_library(root: Path, lib: dict) -> None:
    tmp = root / "library.json.tmp"
    tmp.write_text(json.dumps(lib, indent=1, sort_keys=False))
    tmp.replace(root / "library.json")


def _safe_asset(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    return p if p.is_file() and str(p).startswith(str(root.resolve())) else None


def _reseed(graph: dict) -> dict:
    for node in graph.values():
        ins = node.get("inputs", {})
        for k in ("seed", "noise_seed"):
            if isinstance(ins.get(k), (int, float)):
                ins[k] = random.randrange(2**31)
    return graph


def _fix_graph(ref_name: str, instruction: str, prefix: str) -> dict:
    """The proven Qwen-Image-Edit-2511 wiring, instruction-edit form: the chosen
    output is the sole reference; the user's words are the edit."""
    prompt = (f"{instruction.strip()}. Photorealistic, keep the same model, "
              f"pose, framing, lighting and background; only apply the "
              f"requested change.")
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "2": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen2.5-vl-7b.safetensors", "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-edit-2511.safetensors", "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen-image-vae.safetensors"}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": prompt}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "prompt": _NEG}},
        "8": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["6", 0], "reference_latents_method": "index_timestep_zero"}},
        "9": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["7", 0], "reference_latents_method": "index_timestep_zero"}},
        "10": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1536, "batch_size": 1}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "12": {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}},
        "13": {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["8", 0], "negative": ["9", 0], "latent_image": ["10", 0],
                 "seed": random.randrange(2**31), "steps": 30, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}},
    }


async def _queue_prompt(request: web.Request, server, graph: dict) -> str:
    """Self-POST to the ONE /prompt path with the caller's own credentials, so
    dispatch/billing/tenancy/content-publish behave exactly as a UI run."""
    port = getattr(server, "port", None) or int(os.environ.get("PORT", "8188"))
    headers = {"Content-Type": "application/json"}
    for h in ("Authorization", "Cookie"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(f"http://127.0.0.1:{port}/prompt", json={"prompt": graph}, headers=headers) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise web.HTTPBadGateway(text=json.dumps(body)[:500])
            return body.get("prompt_id", "")


def _workflows(org: str) -> list[str]:
    """Saved workflow files for the org (any user), for the Pipelines tab."""
    base = Path(folder_paths.get_user_directory())
    roots = [base / "orgs" / org / "user", base / "default"]
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for wf in root.rglob("workflows/*/*.json"):
            seen.add(str(wf).split("workflows/", 1)[1])
        for wf in root.rglob("workflows/*.json"):
            seen.add(str(wf).split("workflows/", 1)[1])
    return sorted(seen)


@web.middleware
async def home_redirect(request: web.Request, handler):
    """Deployed default: land on the content view. The editor stays one click
    away (?advanced=1) and every non-root path is untouched."""
    if (_home_default() and request.path == "/" and request.method == "GET"
            and "advanced" not in request.query
            and "text/html" in request.headers.get("Accept", "")):
        raise web.HTTPFound("/studio")
    return await handler(request)


def add_studio_home_routes(routes: web.RouteTableDef, server) -> None:

    @routes.get("/studio")
    async def studio_home(request: web.Request):
        return web.Response(text=_HTML.read_text(), content_type="text/html")

    @routes.get("/v1/library")
    async def get_library(request: web.Request):
        org = _org_of(request)
        root = _library_root(org)
        if root is None:
            return web.json_response({"org": org, "assets": [], "workflows": _workflows(org),
                                      "note": "no library.json for this org yet"})
        lib = _load_library(root)
        assets = []
        for a in lib.get("assets", []):
            p = _safe_asset(root, a.get("path", ""))
            if p is None:
                continue
            row = dict(a)
            row["view"] = _view_params(p)
            row["mtime"] = int(p.stat().st_mtime)
            if p.suffix.lower() == ".png":
                row["prov"] = _provenance(p)
            assets.append(row)
        return web.json_response({"org": org, "assets": assets, "workflows": _workflows(org)})

    @routes.get("/v1/library/file")
    async def get_asset_file(request: web.Request):
        # Org-scoped asset serving. The Studio engine's core /view serves the
        # GLOBAL output dir, so org assets (orgs/<org>/output/…) 404 there → blank
        # thumbnails. This resolves the caller's org and serves from its library root.
        org = _org_of(request)
        root = _library_root(org)
        p = _safe_asset(root, request.query.get("path", "")) if root else None
        if p is None:
            return web.Response(status=404)
        return web.FileResponse(p)

    @routes.post("/v1/library/status")
    async def set_status(request: web.Request):
        body = await request.json()
        rel, status = body.get("path", ""), body.get("status", "")
        if status not in _STATUSES:
            return web.json_response({"error": f"status must be one of {_STATUSES}"}, status=400)
        org = _org_of(request)
        root = _library_root(org)
        if root is None or _safe_asset(root, rel) is None:
            return web.json_response({"error": "unknown asset"}, status=404)
        lib = _load_library(root)
        for a in lib.get("assets", []):
            if a.get("path") == rel:
                a["status"] = status
                a["updatedAt"] = int(time.time())
                _save_library(root, lib)
                return web.json_response({"ok": True, "path": rel, "status": status})
        return web.json_response({"error": "asset not in library"}, status=404)

    @routes.post("/v1/library/rerun")
    async def rerun(request: web.Request):
        body = await request.json()
        org = _org_of(request)
        root = _library_root(org)
        p = _safe_asset(root, body.get("path", "")) if root else None
        if p is None or p.suffix.lower() != ".png":
            return web.json_response({"error": "unknown asset"}, status=404)
        try:
            from PIL import Image
            graph = json.loads(Image.open(p).info.get("prompt", "null"))
        except Exception:
            graph = None
        if not isinstance(graph, dict):
            return web.json_response({"error": "no embedded workflow in this image"}, status=422)
        ids = []
        for _ in range(max(1, min(int(body.get("count", 1)), 4))):
            ids.append(await _queue_prompt(request, server, _reseed(json.loads(json.dumps(graph)))))
        return web.json_response({"ok": True, "prompt_ids": ids})

    @routes.post("/v1/library/fix")
    async def fix(request: web.Request):
        body = await request.json()
        instruction = (body.get("instruction") or "").strip()
        if not 3 <= len(instruction) <= 500:
            return web.json_response({"error": "instruction required (3-500 chars)"}, status=400)
        org = _org_of(request)
        root = _library_root(org)
        p = _safe_asset(root, body.get("path", "")) if root else None
        if p is None or p.suffix.lower() != ".png":
            return web.json_response({"error": "unknown asset"}, status=404)
        # LoadImage reads from the input dir: stage the chosen output there.
        in_dir = Path(folder_paths.get_org_input_directory(org))
        in_dir.mkdir(parents=True, exist_ok=True)
        ref_name = f"fix_src_{abs(hash((str(p), p.stat().st_mtime))) % 10**10}.png"
        (in_dir / ref_name).write_bytes(p.read_bytes())
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", p.stem)[:48]
        pid = await _queue_prompt(request, server, _fix_graph(ref_name, instruction, f"fixes/{stem}"))
        return web.json_response({"ok": True, "prompt_id": pid, "output_prefix": f"fixes/{stem}"})
