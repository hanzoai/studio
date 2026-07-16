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
    POST /v1/library/compose     {paths[], uploads[]?, instruction}  multi-reference render
                                 (library assets and/or images uploaded via /upload/image)

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
from middleware import worklog

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


_REF_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _resolve_uploads(org: str, uploads: list | None) -> list[str]:
    """Validate reference images already uploaded via ``POST /upload/image`` — the
    core engine route lands them in this org's input dir (the SAME place fix/compose
    stage their library-sourced references), so the compose graph's LoadImage nodes
    can name them directly and the BYO-GPU collector ships them with the job. Return
    the safe basenames, in order, de-duped. A traversal/absent/non-image entry is
    dropped, never fatal — the caller enforces the reference count."""
    in_dir = Path(folder_paths.get_org_input_directory(org))
    root = str(in_dir.resolve())
    names: list[str] = []
    for u in uploads or []:
        if not isinstance(u, str):
            continue
        name = os.path.basename(u.strip())
        if not name or name in names or os.path.splitext(name)[1].lower() not in _REF_EXT:
            continue
        p = (in_dir / name).resolve()
        if p.is_file() and str(p).startswith(root):
            names.append(name)
    return names


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


def _compose_graph(ref_names: list[str], instruction: str, prefix: str) -> dict:
    """Multi-reference compose: several chosen assets become the references and the
    user's words say HOW to combine them into a new output. Same proven Qwen
    reference-conditioned wiring as _fix_graph (TextEncodeQwenImageEditPlus takes
    image1..imageN), so no new pipeline — the multi-input case of the good graph."""
    prompt = (f"{instruction.strip()}. Combine the provided reference images as "
              f"described. Photorealistic, coherent single result.")
    g: dict = {
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen2.5-vl-7b.safetensors", "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-edit-2511.safetensors", "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen-image-vae.safetensors"}},
    }
    pos_inputs: dict = {"clip": ["3", 0], "vae": ["5", 0], "prompt": prompt}
    nid = 20
    for i, name in enumerate(ref_names[:5], start=1):  # up to 5 references
        li, si = str(nid), str(nid + 1)
        g[li] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[si] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [li, 0]}}
        pos_inputs[f"image{i}"] = [si, 0]
        nid += 2
    g["6"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": pos_inputs}
    g["7"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "prompt": _NEG}}
    g["8"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["6", 0], "reference_latents_method": "index_timestep_zero"}}
    g["9"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["7", 0], "reference_latents_method": "index_timestep_zero"}}
    g["10"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1536, "batch_size": 1}}
    g["11"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}}
    g["12"] = {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}}
    g["13"] = {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["8", 0], "negative": ["9", 0], "latent_image": ["10", 0],
               "seed": random.randrange(2**31), "steps": 30, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["14"] = {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}}
    g["15"] = {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}}
    return g


class DispatchError(ValueError):
    """A create/fix/compose request that can't be honored — bad input, unknown
    asset, or a downstream /prompt failure. Carries a human message; the routes
    return it as ``{"error": <message>}`` so a dispatch NEVER fails silently."""


def _prompt_error_message(body) -> str:
    """A short, human reason from a /prompt non-200 body. On a model-less cloud
    pod the graph fails validation ("unet_name '…' not in []"); the router returns
    'no GPU worker' / 'provisioning'. This is what the user must SEE instead of a
    silent nothing."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or "render could not be queued"
        det = err.get("details")
        return f"{msg} — {det}" if det and det != msg else msg
    if isinstance(err, str) and err.strip():
        return err.strip()
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return "render could not be queued — no GPU worker online for this org"


async def _queue_prompt(request: web.Request, server, graph: dict) -> str:
    """Self-POST to the ONE /prompt path with the caller's own credentials, so
    dispatch/billing/tenancy/content-publish behave exactly as a UI run. A non-200
    from /prompt (model-less pod fails validation; router has no GPU worker) is
    raised as a ``DispatchError`` carrying the real reason — NOT a raw 502 whose
    truncated body the dialog can't parse (that was the deployed 'nothing happened /
    nothing queued' — the error was swallowed)."""
    port = getattr(server, "port", None) or int(os.environ.get("PORT", "8188"))
    headers = {"Content-Type": "application/json"}
    for h in ("Authorization", "Cookie"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(f"http://127.0.0.1:{port}/prompt", json={"prompt": graph}, headers=headers) as r:
                text = await r.text()
                try:
                    body = json.loads(text) if text else {}
                except ValueError:
                    body = {}   # non-JSON (e.g. an HTML error page) — don't 500, report it
                if r.status != 200:
                    raise DispatchError(_prompt_error_message(body) if body
                                        else f"render engine returned HTTP {r.status}")
                pid = body.get("prompt_id", "") if isinstance(body, dict) else ""
                if not pid:
                    raise DispatchError("render was not queued (no prompt_id returned)")
                return pid
    except aiohttp.ClientError as e:
        raise DispatchError(f"could not reach the render engine ({e})")


def _graph_params(graph: dict) -> dict:
    """The render params worth showing in the work-log context panel."""
    p: dict = {}
    for n in graph.values():
        if not isinstance(n, dict):
            continue
        ct, ins = n.get("class_type", ""), n.get("inputs", {})
        if "KSampler" in ct:
            p.update(steps=ins.get("steps"), cfg=ins.get("cfg"),
                     sampler=ins.get("sampler_name"), scheduler=ins.get("scheduler"),
                     seed=ins.get("seed", ins.get("noise_seed")))
        elif ct == "EmptySD3LatentImage":
            p.update(width=ins.get("width"), height=ins.get("height"))
    return {k: v for k, v in p.items() if v is not None}


def _in_render_jobs(org: str, pid: str) -> bool:
    """True if a prompt_id was dispatched to this org's BYO-GPU lane (gpu-jobs) —
    gpu_dispatch._track_dispatched writes it to render_jobs.json during the self-POST."""
    return _render_job(org, pid) is not None


def _render_job(org: str, pid: str) -> dict | None:
    try:
        jf = Path(folder_paths.get_org_output_directory(org)) / "render_jobs.json"
        for j in json.loads(jf.read_text()):
            if j.get("id") == pid:
                return j
    except Exception:
        pass
    return None


def _render_job_node(org: str, pid: str) -> str:
    """The node label the gpu-jobs lane recorded for this dispatch (spark/dbc/…)."""
    j = _render_job(org, pid)
    return (j.get("node") if j else None) or "gpu"


def _output_mtime(root: Path | None, rel: str) -> int | None:
    try:
        return int((root / rel).stat().st_mtime) if root and rel else None
    except OSError:
        return None


def _remove_render_job(org: str, pid: str) -> bool:
    """Drop a BYO-GPU tracking row from this org's render_jobs.json. The file is
    per-org so a match is inherently owned. Returns True if one was removed."""
    try:
        jf = Path(folder_paths.get_org_output_directory(org)) / "render_jobs.json"
        jobs = json.loads(jf.read_text())
    except Exception:
        return False
    keep = [j for j in jobs if j.get("id") != pid]
    if len(keep) == len(jobs):
        return False
    tmp = jf.with_name(jf.name + ".tmp")
    tmp.write_text(json.dumps(keep))
    tmp.replace(jf)
    return True


def _owns_job(server, org: str, pid: str) -> bool:
    """Does this prompt_id belong to the caller's org? True if it's in the org's
    work log, or a local-queue item stamped with this org, or the org's BYO-GPU
    tracking file. A cross-tenant id matches none of these → the route 404s and
    never reveals that it exists for someone else."""
    if any(r.get("id") == pid for r in worklog.load(org)):
        return True
    try:
        running, pending = server.prompt_queue.get_current_queue_volatile()
        if any(it[1] == pid and (it[3] or {}).get("org_id") == org
               for it in list(running) + list(pending)):
            return True
    except Exception:
        pass
    return _in_render_jobs(org, pid)


def _cancel_job(server, org: str, pid: str) -> str:
    """Cancel an OWNED job across both realms (caller must have checked ownership).
    Local pod queue: delete a pending item, or interrupt a running one — both
    org-scoped so one tenant can't touch another's render. BYO-GPU: drop the
    tracking row (a queued gpu-job stops showing; one already rendering on the
    worker may still finish — the pod can't stop a remote process). Marks the work
    log cancelled. Returns the realm acted on."""
    realm = "gone"
    try:
        running, pending = server.prompt_queue.get_current_queue_volatile()
    except Exception:
        running, pending = [], []
    if any(it[1] == pid and (it[3] or {}).get("org_id") == org for it in pending):
        server.prompt_queue.delete_queue_item(
            lambda it: it[1] == pid and (it[3] or {}).get("org_id") == org)
        realm = "local"
    elif any(it[1] == pid and (it[3] or {}).get("org_id") == org for it in running):
        try:
            import nodes
            nodes.interrupt_processing()
            realm = "local"
        except Exception:
            pass
    if _remove_render_job(org, pid) and realm == "gone":
        realm = "gpu"
    worklog.set_status(org, pid, "cancelled")
    return realm


def _landed_output(root: Path | None, prefix: str) -> str | None:
    """Library-relative path of the newest file a SaveImage prefix produced, or None
    if it hasn't landed yet. Lets the work-log report done + wire lineage edges."""
    if root is None or not prefix:
        return None
    try:
        parent = root / os.path.dirname(prefix)
        stem = os.path.basename(prefix)
        # Match SaveImage's exact "<prefix>_<counter>_.png" — NOT a loose "<prefix>*"
        # which also matches a CHILD prefix ("a" would swallow "a_00001__00001_"),
        # collapsing distinct lineage steps onto one output.
        pat = re.compile(r"^" + re.escape(stem) + r"_\d+_\.png$")
        hits = [p for p in parent.glob(stem + "_*_.png") if pat.match(p.name)]
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return str(hits[0].relative_to(root))
    except Exception:
        pass
    return None


async def _dispatch(request, server, org: str, graph: dict, *, kind: str, prompt: str,
                    refs: list, uploads: list, parents: list, output_prefix: str) -> str:
    """The ONE place a render is queued AND recorded. On success writes a `queued`
    work-log row (lane detected); on failure writes a `failed` row with the reason —
    so a dispatch is NEVER invisible, whatever lane it took or however it failed."""
    common = dict(kind=kind, prompt=prompt, refs=refs, uploads=uploads,
                  parents=parents, output_prefix=output_prefix, params=_graph_params(graph))
    try:
        pid = await _queue_prompt(request, server, graph)
    except DispatchError as e:
        worklog.record(org, status="failed", error=str(e), **common)
        raise
    lane = "gpu" if _in_render_jobs(org, pid) else "local"
    node = _render_job_node(org, pid) if lane == "gpu" else "studio pod"
    worklog.record(org, pid=pid, status="queued", lane=lane, node=node, **common)
    return pid


async def dispatch_fix(request: web.Request, server, org: str, rel: str,
                       instruction: str, upload: str | None = None) -> dict:
    """Run the proven Qwen single-image edit. The base image is EITHER a library
    asset (``rel``, staged into the org input dir) OR an image the caller just
    uploaded via ``/upload/image`` (``upload``, already resident there) — so a fresh
    photo can be edited without first landing it in the library. The ONE fix path:
    the /v1/library/fix route and the chat `create` capability both call this."""
    instruction = (instruction or "").strip()
    if not 3 <= len(instruction) <= 500:
        raise DispatchError("instruction required (3-500 chars)")
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    if upload:
        names = _resolve_uploads(org, [upload])
        if not names:
            raise DispatchError(f"unknown upload: {upload}")
        ref_name = names[0]
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(ref_name).stem)[:48]
    else:
        root = _library_root(org)
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        ref_name = f"fix_src_{abs(hash((str(p), p.stat().st_mtime))) % 10**10}.png"
        (in_dir / ref_name).write_bytes(p.read_bytes())
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", p.stem)[:48]
    pid = await _dispatch(request, server, org, _fix_graph(ref_name, instruction, f"fixes/{stem}"),
                          kind="fix", prompt=instruction, refs=[ref_name],
                          uploads=[upload] if upload else [], parents=[] if upload else [rel],
                          output_prefix=f"fixes/{stem}")
    return {"ok": True, "prompt_id": pid, "output_prefix": f"fixes/{stem}"}


async def dispatch_compose(request: web.Request, server, org: str, paths: list,
                           instruction: str, uploads: list | None = None) -> dict:
    """Stage 2-5 references and run the multi-input Qwen graph. References are
    library assets (``paths``, staged into the org input dir) and/or images the
    caller just uploaded via ``/upload/image`` (``uploads``, already resident in
    that dir). Shared by the /v1/library/compose route, the Fix surface's
    add-reference flow, and the chat `create` capability."""
    instruction = (instruction or "").strip()
    if not 3 <= len(instruction) <= 500:
        raise DispatchError("instruction required (3-500 chars)")
    paths = paths or []
    uploaded = _resolve_uploads(org, uploads)
    if not 2 <= len(paths) + len(uploaded) <= 5:
        raise DispatchError("select 2-5 references to compose")
    root = _library_root(org)
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    ref_names = []
    for rel in paths:
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        nm = f"compose_src_{abs(hash((str(p), p.stat().st_mtime))) % 10**10}.png"
        (in_dir / nm).write_bytes(p.read_bytes())
        ref_names.append(nm)
    ref_names.extend(uploaded)
    labels = [Path(x).stem for x in paths] + [Path(u).stem for u in uploaded]
    stem = "compose_" + "_".join(re.sub(r"[^a-zA-Z0-9]", "", s)[:12] for s in labels[:2])
    pid = await _dispatch(request, server, org, _compose_graph(ref_names, instruction, f"composes/{stem}"),
                          kind="compose", prompt=instruction, refs=ref_names,
                          uploads=uploaded, parents=list(paths), output_prefix=f"composes/{stem}")
    return {"ok": True, "prompt_id": pid, "output_prefix": f"composes/{stem}", "refs": len(ref_names)}


class NotOwned(DispatchError):
    """The prompt_id isn't the caller's — routes map this to 404 (never reveals it)."""


async def dispatch_amend(request: web.Request, server, org: str, prompt_id: str,
                         instruction: str, paths: list | None = None,
                         uploads: list | None = None) -> dict:
    """Amend a still-queued job as an honest ATOMIC SWAP: queued graphs are
    immutable, so cancel the original and re-dispatch with the amended prompt and an
    augmented reference set. The original's references (already staged in the org
    input dir, read from its work-log row — the authoritative ownership record) are
    kept and combined with any new library picks (``paths``) and uploads
    (``uploads``). The re-dispatch goes through the ONE funnel, so it lands in the
    right lane and records a fresh work-log row. Position resets (it re-enters the
    queue) — the caller SHOWS that honestly."""
    instruction = (instruction or "").strip()
    if not 3 <= len(instruction) <= 500:
        raise DispatchError("instruction required (3-500 chars)")
    orig = next((r for r in worklog.load(org) if r.get("id") == prompt_id), None)
    if orig is None:
        raise NotOwned(f"no such job: {prompt_id}")
    if orig.get("status") in ("done", "failed", "cancelled"):
        raise DispatchError("this job already finished — start a new fix instead")
    root = _library_root(org)
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    # keep the original references that are still present (already input-dir names)
    ref_names = _resolve_uploads(org, orig.get("refs") or [])
    new_uploads = _resolve_uploads(org, uploads)
    for rel in (paths or []):
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        nm = f"amend_src_{abs(hash((str(p), p.stat().st_mtime))) % 10**10}.png"
        (in_dir / nm).write_bytes(p.read_bytes())
        ref_names.append(nm)
    # de-dupe, keep order, cap at the graph's 5
    seen: set = set()
    refs = [n for n in ref_names + new_uploads if not (n in seen or seen.add(n))][:5]
    if not refs:
        raise DispatchError("no reference images available to amend")
    _cancel_job(server, org, prompt_id)
    parents = list(orig.get("parents") or []) + list(paths or [])
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(refs[0]).stem)[:40] + "_amended"
    if len(refs) == 1:
        graph, kind, out = _fix_graph(refs[0], instruction, f"fixes/{stem}"), "fix", f"fixes/{stem}"
    else:
        graph, kind, out = _compose_graph(refs, instruction, f"composes/{stem}"), "compose", f"composes/{stem}"
    all_uploads = list(dict.fromkeys(list(orig.get("uploads") or []) + new_uploads))
    pid = await _dispatch(request, server, org, graph, kind=kind, prompt=instruction,
                          refs=refs, uploads=all_uploads, parents=parents, output_prefix=out)
    return {"ok": True, "prompt_id": pid, "amended_from": prompt_id,
            "output_prefix": out, "refs": len(refs)}


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
        # no-store: the SPA HTML changes every release; without this, browsers
        # (and any proxy) heuristically cache it and serve a STALE page after a
        # deploy — which is why UI fixes (e.g. delete) appeared "not working" to
        # a user still running yesterday's cached studio_home.html. The page is
        # tiny; always serve fresh. Static assets keep their own long cache.
        return web.Response(text=_HTML.read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-store, must-revalidate"})

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
        # ?thumb=1 → a cached, downsized JPEG (grid loads full-res 1-2MB PNGs one by
        # one otherwise, which makes the whole page crawl). Full-res on click/zoom.
        org = _org_of(request)
        root = _library_root(org)
        # ?input=1 → a staged reference in orgs/<org>/input (fix/compose sources),
        # so YouTube-style queue rows can show the referenced work as a thumbnail.
        if request.query.get("input"):
            in_dir = Path(folder_paths.get_org_input_directory(org))
            name = os.path.basename(request.query.get("path", ""))
            ip = in_dir / name
            if not name or not ip.is_file() or not str(ip.resolve()).startswith(str(in_dir.resolve())):
                return web.Response(status=404)
            p = ip
        else:
            p = _safe_asset(root, request.query.get("path", "")) if root else None
            if p is None:
                return web.Response(status=404)
        if request.query.get("thumb") and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            try:
                tdir = root / ".thumbs"
                tdir.mkdir(exist_ok=True)
                key = re.sub(r"[^a-zA-Z0-9]", "_", str(p))[-120:]
                tp = tdir / f"{key}.jpg"
                if not tp.is_file() or tp.stat().st_mtime < p.stat().st_mtime:
                    from PIL import Image
                    im = Image.open(p)
                    im.thumbnail((480, 720))  # grid-sized; keeps aspect
                    im.convert("RGB").save(tp, "JPEG", quality=82)
                return web.FileResponse(tp, headers={"Cache-Control": "public, max-age=86400"})
            except Exception:
                pass  # fall through to full-res on any thumbnail failure
        return web.FileResponse(p)

    @routes.get("/v1/gpu-status")
    async def gpu_status(request: web.Request):
        """Does the caller's org have an online GPU to render on? The UI showed
        'connect a GPU' even with a GPU connected because it checked /v1/engines
        (the worker-federation) — but `hanzo gpu connect` registers with the FLEET
        (gpu-jobs), which is exactly what dispatch uses. Report the SAME source of
        truth as dispatch (_has_online_gpu) so the UI matches what actually works."""
        try:
            from middleware.gpu_dispatch import _bearer, _has_online_gpu
        except ImportError:
            from .middleware.gpu_dispatch import _bearer, _has_online_gpu
        try:
            tok = _bearer(request)
            online = bool(tok and _has_online_gpu(tok))
        except Exception:
            online = False
        return web.json_response({"online": online})

    @routes.get("/v1/render-queue")
    async def render_queue(request: web.Request):
        """The org's IN-FLIGHT BYO-GPU renders (fix/compose/rerun dispatched to a
        connected GPU). These run on the worker's gpu-jobs queue, NOT the pod's
        local /queue, so the UI could not see them — this is what makes a queued
        fix visible. Drops entries whose output has landed in the library or that
        have aged out (~25 min). Cheap: reads one small per-org JSON."""
        org = _org_of(request)
        root = _library_root(org)
        if root is None:
            return web.json_response({"jobs": []})
        jf = root / "render_jobs.json"
        try:
            jobs = json.loads(jf.read_text())
        except Exception:
            jobs = []
        now = int(time.time())
        live = []
        for j in jobs:
            age = now - int(j.get("ts", 0))
            if age > 1500:            # aged out (worker deadline is 1200s)
                continue
            # Completed only when the ACTUAL output for THIS job exists: the render
            # saves to "<outPrefix>_NNNNN_.png". Check precisely (glob the exact
            # output prefix) — not a fuzzy base match, which collided with the source
            # image and made the job disappear from the queue after 30s.
            out_prefix = j.get("outPrefix") or j.get("prefix") or ""
            landed = False
            if out_prefix:
                try:
                    parent = root / os.path.dirname(out_prefix)
                    stem = os.path.basename(out_prefix)
                    landed = any(parent.glob(stem + "*.png"))
                except Exception:
                    landed = False
            if landed and age > 20:
                continue
            j["age"] = age
            live.append(j)
        return web.json_response({"jobs": live})

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
        rel = body.get("path", "")
        out_prefix = ""
        instr = ""
        for n in graph.values():
            if not isinstance(n, dict):
                continue
            if n.get("class_type") == "SaveImage":
                out_prefix = n.get("inputs", {}).get("filename_prefix", "") or out_prefix
            elif "TextEncode" in n.get("class_type", "") and not instr:
                t = n.get("inputs", {}).get("prompt") or n.get("inputs", {}).get("text") or ""
                if t and t != _NEG:
                    instr = t
        ids = []
        try:
            for _ in range(max(1, min(int(body.get("count", 1)), 4))):
                ids.append(await _dispatch(
                    request, server, org, _reseed(json.loads(json.dumps(graph))),
                    kind="rerun", prompt=instr, refs=[], uploads=[], parents=[rel],
                    output_prefix=out_prefix))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "prompt_ids": ids})

    @routes.post("/v1/queue/cancel")
    async def cancel_job(request: web.Request):
        """Cancel one of the caller's OWN queued/running jobs (local pod queue or
        BYO-GPU lane). Ownership is resolved server-side; a prompt_id that isn't the
        caller's 404s — never confirming another tenant's job exists."""
        org = _org_of(request)
        pid = (await request.json()).get("prompt_id", "")
        if not pid:
            return web.json_response({"error": "prompt_id required"}, status=400)
        if not _owns_job(server, org, pid):
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True, "canceled": _cancel_job(server, org, pid)})

    @routes.post("/v1/queue/amend")
    async def amend_job(request: web.Request):
        """Amend a still-queued job (atomic cancel + re-dispatch with more context)."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_amend(
                request, server, _org_of(request), body.get("prompt_id", ""),
                body.get("instruction", ""), body.get("paths"), body.get("uploads")))
        except NotOwned as e:
            return web.json_response({"error": str(e)}, status=404)
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.get("/v1/queue/status")
    async def queue_status(request: web.Request):
        """This org's LIVE queue with where + when: per-lane position N of M, the
        node each job runs on, elapsed for the running item, and an ETA computed
        server-side from the org's OWN measured render medians. Tenant-scoped — only
        the caller's active jobs and their positions. Cheap: reads the org work log,
        stamps started/finished as it observes them, no fleet call."""
        org = _org_of(request)
        root = _library_root(org)
        now = int(time.time())
        rows = worklog.load(org)
        # finalize any job whose output has landed (real finish time = file mtime)
        for r in rows:
            if r.get("status") in ("done", "failed", "cancelled"):
                continue
            landed = _landed_output(root, r.get("output_prefix"))
            if landed:
                mt = _output_mtime(root, landed) or now
                worklog.mark_finished(org, r["id"], mt)
                r["status"], r["finished_at"] = "done", mt   # mirror in-memory
        active = [r for r in rows if r.get("status") not in ("done", "failed", "cancelled")
                  and now - int(r.get("ts", now)) <= 1800]
        # the oldest job in each lane is the one rendering — stamp its start once
        by_lane: dict[str, list[dict]] = {}
        for r in active:
            by_lane.setdefault(r.get("lane") or "local", []).append(r)
        for jobs in by_lane.values():
            head = min(jobs, key=lambda j: j.get("ts", 0))
            if not head.get("started_at"):
                worklog.mark_started(org, head["id"], now)
                head["started_at"] = now
            head["status"] = "running"
        medians = worklog.median_durations(rows)
        est, counts = worklog.estimate_lanes(active, medians, now)
        items = {}
        for r in active:
            items[r["id"]] = {**est.get(r["id"], {}), "id": r["id"], "kind": r.get("kind", ""),
                              "prompt": r.get("prompt", ""), "status": r.get("status"),
                              "node": r.get("node")}
        return web.json_response({"items": items, "medians": medians, "lanes": counts, "now": now})

    @routes.get("/v1/nodes")
    async def get_nodes(request: web.Request):
        """The caller-org's GPU nodes (online/offline) for the queue header badges,
        plus this studio pod. The fleet is token-scoped, so only the caller's org's
        nodes are ever returned."""
        try:
            from middleware.gpu_dispatch import _bearer, _fleet_machines, _node_label
        except ImportError:
            from .middleware.gpu_dispatch import _bearer, _fleet_machines, _node_label
        nodes = []
        try:
            tok = _bearer(request)
            for m in (_fleet_machines(tok) if tok else []):
                if not m.get("gpu"):
                    continue
                nodes.append({"name": _node_label(m), "online": m.get("status") == "online"})
        except Exception:
            nodes = []
        return web.json_response({"nodes": nodes, "pod": {"name": "studio pod", "online": True}})

    @routes.get("/v1/worklog")
    async def get_worklog(request: web.Request):
        """This org's dispatched renders (fix/compose/rerun) with REAL status — the
        queue/history backbone. Reverse-chronological; ?q= filters over prompt+kind.
        Server-resolved org: a caller only ever sees its OWN log file."""
        org = _org_of(request)
        root = _library_root(org)
        rows = worklog.load(org)
        out = []
        for r in reversed(rows):
            row = dict(r)
            landed = _landed_output(root, r.get("output_prefix"))
            row["output"] = landed
            if landed and r.get("status") not in ("failed", "cancelled"):
                row["status"] = "done"
            out.append(row)
        q = (request.query.get("q") or "").lower().strip()
        if q:
            out = [r for r in out if q in (r.get("prompt", "") or "").lower() or q in (r.get("kind", "") or "")]
        return web.json_response({"org": org, "items": out[:200]})

    @routes.get("/v1/worklog/lineage")
    async def get_lineage(request: web.Request):
        """The version chain for one library asset — ancestors (what it was derived
        from, with the prompt at each step) and descendants (later fixes of it).
        Pure over THIS org's rows; cross-tenant lineage is impossible by construction."""
        org = _org_of(request)
        root = _library_root(org)
        target = request.query.get("path", "")
        rows = worklog.load(org)
        for r in rows:
            r["output"] = _landed_output(root, r.get("output_prefix"))
        return web.json_response(worklog.build_lineage(rows, target))

    @routes.post("/v1/library/fix")
    async def fix(request: web.Request):
        body = await request.json()
        try:
            return web.json_response(await dispatch_fix(
                request, server, _org_of(request), body.get("path", ""),
                body.get("instruction", ""), body.get("upload")))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/v1/library/compose")
    async def compose(request: web.Request):
        """Select N references (library assets and/or freshly uploaded images) + say
        HOW to combine them → a new generated output. The multi-input case of the
        proven Qwen reference-conditioned graph."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_compose(
                request, server, _org_of(request), body.get("paths") or [],
                body.get("instruction", ""), body.get("uploads")))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    # ── Ratings + notes: the curation feedback loop (HIP-0506 §5) ──────────────
    # Stored per-org in orgs/<org>/output/ratings.json keyed by asset path OR run
    # prompt_id. 0-5 stars + free notes; the copilot reads these before assembling
    # a workflow so generations get closer to intent each pass.
    @routes.get("/v1/library/ratings")
    async def get_ratings(request: web.Request):
        root = _library_root(_org_of(request))
        if root is None:
            return web.json_response({})
        try:
            return web.json_response(json.loads((root / "ratings.json").read_text()))
        except Exception:
            return web.json_response({})

    @routes.post("/v1/library/rate")
    async def rate(request: web.Request):
        body = await request.json()
        key = (body.get("key") or "").strip()   # asset path or run prompt_id
        if not key:
            return web.json_response({"error": "key required"}, status=400)
        stars = body.get("stars")
        if stars is not None and not (isinstance(stars, int) and 0 <= stars <= 5):
            return web.json_response({"error": "stars must be 0-5"}, status=400)
        root = _library_root(_org_of(request))
        if root is None:
            return web.json_response({"error": "no library for org"}, status=404)
        rf = root / "ratings.json"
        try:
            data = json.loads(rf.read_text())
        except Exception:
            data = {}
        entry = data.get(key, {})
        if stars is not None:
            entry["stars"] = stars
        if "notes" in body:
            entry["notes"] = str(body.get("notes") or "")[:2000]
        if body.get("favorite") is not None:
            entry["favorite"] = bool(body.get("favorite"))
        entry["updatedAt"] = int(time.time())
        data[key] = entry
        tmp = root / "ratings.json.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(rf)
        return web.json_response({"ok": True, "key": key, "entry": entry})
