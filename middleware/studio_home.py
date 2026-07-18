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
    POST /v1/library/dedup       soft-delete exact-duplicate images (recoverable)
    POST /v1/library/purge       {paths[] | all}  permanently remove ARCHIVED assets —
                                 unlink file + drop record, irreversible, archive-only

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
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

import folder_paths
from middleware import quality_gate, stacks_store, worklog

log = logging.getLogger("studio.home")

_HTML = Path(__file__).with_name("studio_home.html")
_ASSET_DIR = Path(__file__).parent
_STATUSES = ("draft", "approved", "queued", "published", "deleted", "flagged")
_PROV_CACHE: dict[str, tuple[float, dict]] = {}  # abspath -> (mtime, provenance)

_NEG = ("ghosting, double exposure, duplicated body, transparent fabric, sheer "
        "fabric, illustration, painterly, blotchy skin, mottled skin, jpeg "
        "artifacts, pixelation, noise, deformed hands, extra limbs, text, watermark")

# The Qwen-Image-Edit-2511 model set, by the EXACT filenames present on the render
# worker (diffusion_models / text_encoders / vae — verified on spark). The graph
# names ONLY these: a graph that referenced a missing checkpoint fails ComfyUI
# validation instantly and lands a degenerate, un-sampled result.
_QWEN_UNET = "qwen-image-edit-2511.safetensors"
_QWEN_CLIP = "qwen2.5-vl-7b.safetensors"
_QWEN_VAE = "qwen-image-vae.safetensors"


def _home_default() -> bool:
    return os.environ.get("STUDIO_HOME_DEFAULT", "").lower() in ("1", "true", "yes")


def _org_of(request: web.Request) -> str:
    user = request.get("iam_user") or {}
    return user.get("org_id") or os.environ.get("STUDIO_ORG_ID") or "default"


def _owner_of(request: web.Request) -> str:
    """The acting user (IAM subject) — a Stack's owner_id / added_by. Best-effort;
    Stacks are org-scoped, so this is provenance, not an access boundary."""
    return (request.get("iam_user") or {}).get("sub") or ""


def _workspace_of(request: web.Request) -> str | None:
    """The active project = a Stack's workspace_id, or None when unscoped (the
    same ``studio_active_project`` cookie the session/workflow scope reads)."""
    return request.cookies.get("studio_active_project") or None


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


def _soft_delete_assets(org: str, asset_ids: list[str]) -> None:
    """Trash a set of library assets (status=deleted) — the SAME soft-delete the
    gallery's 🗑 uses. Files are never unlinked; Deleted stays recoverable. This is
    what "Delete Stack and Images" maps to (Stacks own no lifecycle of their own)."""
    root = _library_root(org)
    if root is None:
        return
    ids = set(asset_ids)
    lib = _load_library(root)
    changed = False
    for a in lib.get("assets", []):
        if a.get("path") in ids and a.get("status") != "deleted":
            a["status"] = "deleted"
            a["updatedAt"] = int(time.time())
            changed = True
    if changed:
        _save_library(root, lib)


def _safe_asset(root: Path, rel: str) -> Path | None:
    if any(part.startswith(".") for part in Path(rel).parts):
        # Hidden segments are never assets — AppleDouble forks AND cache
        # trees (.thumbs/) in one rule, refused everywhere at once.
        return None
    p = (root / rel).resolve()
    return p if p.is_file() and str(p).startswith(str(root.resolve())) else None


def _drop_thumb(root: Path, rel: str) -> None:
    """Remove an asset's cached grid thumbnail, if one exists. Mirrors the key scheme
    get_asset_file writes (sanitized resolved path → root/.thumbs/<key>.jpg) so a
    permanent delete leaves no orphaned thumbnail behind. Best-effort."""
    try:
        p = (root / rel).resolve()
        key = re.sub(r"[^a-zA-Z0-9]", "_", str(p))[-120:]
        (root / ".thumbs" / f"{key}.jpg").unlink(missing_ok=True)
    except OSError:
        pass


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
    """The canonical Qwen-Image-Edit-2511 instruction edit — ComfyUI's OWN
    image_qwen_image_edit template, node for node. FluxKontextImageScale sizes the
    reference ONCE; that SAME scaled image feeds both TextEncodeQwenImageEditPlus
    (the edit conditioning) and VAEEncode (the sampling latent), so reference and
    generation latents are identical in size. Denoise 1.0 over that latent applies
    the edit; AuraFlow shift + CFGNorm are the recipe.

    The prod graph broke it in one place: two FluxKontextMultiReferenceLatentMethod
    nodes forced a Flux-Kontext `index_timestep_zero` reference method onto Qwen
    conditioning. That baked latent-grid mosaic + skin speckle — the noise-stipple
    class — into every edit and compounded on each fix-of-a-fix. Gone here: Qwen's
    native reference handling (TextEncodeQwenImageEditPlus alone) renders clean.
    Feeding the SAME scaled image to conditioning and latent is what also keeps the
    generation from insetting the scene in a frame-within-frame border."""
    prompt = (f"{instruction.strip()}. Photorealistic, keep the same model, "
              f"pose, framing, lighting and background; only apply the "
              f"requested change.")
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "2": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": _QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": _QWEN_UNET, "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _QWEN_VAE}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": prompt}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": _NEG}},
        "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["5", 0]}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "12": {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}},
        "13": {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0],
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
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": _QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": _QWEN_UNET, "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _QWEN_VAE}},
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
    # Qwen's native multi-reference conditioning (image1..imageN on node 6) — NO
    # FluxKontextMultiReferenceLatentMethod: the same Flux-Kontext override that
    # stippled the single-image fix has no place on Qwen conditioning here either.
    g["10"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1536, "batch_size": 1}}
    g["11"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}}
    g["12"] = {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}}
    g["13"] = {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0],
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


def _fix_source_ref(out_dir: Path, subfolder: str, name: str) -> str | None:
    """The source ref staged for the fix output <subfolder>/<name>, read from the
    render-jobs record (outPrefix → refs[0]) gpu_dispatch writes on dispatch. None
    if the output is not a tracked fix or the record is gone."""
    try:
        jobs = json.loads((out_dir / "render_jobs.json").read_text())
    except Exception:
        return None
    rel = f"{subfolder}/{name}" if subfolder else name
    for j in jobs:
        op, refs = j.get("outPrefix") or "", j.get("refs") or []
        if op and refs and rel.startswith(op):
            return refs[0]
    return None


def colormatch_fix_ingest(request, subfolder: str, name: str) -> None:
    """When a BYO-GPU worker uploads a finished FIX render, restore its global colour
    to the source — undoing the white-balance/exposure/saturation drift a full-frame
    Qwen edit (denoise 1.0) introduces, and its fix-of-a-fix compounding. Scoped to
    ``fixes/…`` outputs only (never compose/normal renders) whose staged source ref
    is still present; a render that did not drift is matched to itself = untouched.
    Fail-safe: never raises, and leaves the file exactly as uploaded on any doubt."""
    try:
        sub, nm = subfolder or "", name or ""
        if "fixes" not in sub and "fixes/" not in nm:
            return
        org = _org_of(request)
        out_dir = Path(folder_paths.get_org_output_directory(org))
        src_ref = _fix_source_ref(out_dir, sub, nm)
        if not src_ref:
            return
        src_path = Path(folder_paths.get_org_input_directory(org)) / src_ref
        from middleware import color_fidelity
        color_fidelity.match_in_place(str(out_dir / sub / nm), str(src_path))
    except Exception:
        pass


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
                       instruction: str, upload: str | None = None,
                       stack: str | None = None) -> dict:
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
    note = None
    if upload:
        names = _resolve_uploads(org, [upload])
        if not names:
            raise DispatchError(f"unknown upload: {upload}")
        ref_name = names[0]
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(ref_name).stem)[:48]
    else:
        root = _library_root(org)
        # Iteration hygiene: if the chosen base is corrupt (flagged/deleted), fix from
        # its nearest clean ancestor instead — never compound a known-bad version.
        rel, substituted = _clean_base(org, root, rel)
        if substituted:
            note = "Fixing from the original — this version is corrupted"
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
    if stack:
        stacks_store.route_prefix(org, f"fixes/{stem}", stack, added_by=_owner_of(request))
    out = {"ok": True, "prompt_id": pid, "output_prefix": f"fixes/{stem}"}
    if note:
        out["note"] = note
    return out


async def dispatch_compose(request: web.Request, server, org: str, paths: list,
                           instruction: str, uploads: list | None = None,
                           stack: str | None = None) -> dict:
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
    if stack:
        stacks_store.route_prefix(org, f"composes/{stem}", stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "output_prefix": f"composes/{stem}", "refs": len(ref_names)}


class NotOwned(DispatchError):
    """The prompt_id isn't the caller's — routes map this to 404 (never reveals it)."""


async def dispatch_amend(request: web.Request, server, org: str, prompt_id: str,
                         instruction: str, paths: list | None = None,
                         uploads: list | None = None, stack: str | None = None) -> dict:
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
    if stack:
        stacks_store.route_prefix(org, out, stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "amended_from": prompt_id,
            "output_prefix": out, "refs": len(refs)}


def _short_prompt(prompt: str) -> str:
    """A few words of a prompt → a readable auto-stack name stem."""
    words = re.findall(r"[A-Za-z0-9']+", prompt or "")
    return (" ".join(words[:6])[:48]).strip() or "Batch"


def _dest_stack(org: str, stack, *, prompt: str, multi: bool, owner: str, workspace) -> str | None:
    """Resolve where a dispatch's outputs are organized. An explicit Stack id (the
    'Save outputs to → Existing/New Stack' choice, the client having created a New
    Stack first) wins. Otherwise, when the org's 'auto-stack batch generations'
    setting is on AND the job is multi-output, mint a '<short prompt> — <date>'
    Stack. Returns the Stack id, or None (Main Gallery / single output / setting off)."""
    sid = (stack or "").strip() if isinstance(stack, str) else None
    if sid:
        return sid
    if multi and stacks_store.get_setting(org, "auto_stack_batches", False):
        name = f"{_short_prompt(prompt)} — {time.strftime('%Y-%m-%d')}"
        return stacks_store.create(org, name, [], owner=owner, workspace=workspace)["id"]
    return None


def _graph_is_batch(graph: dict) -> bool:
    """A graph whose latent asks for more than one image — a multi-output job."""
    for n in (graph or {}).values():
        if isinstance(n, dict) and int(n.get("inputs", {}).get("batch_size", 1) or 1) > 1:
            return True
    return False


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


# ── Shared shell: the ONE chrome (Studio/Editor toggle, wallet, GPUs, chat
# sidebar) loaded by BOTH views. The Studio home <script>-includes it; the Editor
# gets it injected into the engine's index.html by server.get_root. ──────────────
_SHELL_HEAD = '<link rel="stylesheet" href="/studio/shell.css">'
_SHELL_BODY = '<script src="/studio/shell.js" defer></script>'


def _asset(name: str, ctype: str) -> web.Response:
    """Serve a shell asset. no-store like the SPA HTML — it changes every release."""
    return web.Response(text=(_ASSET_DIR / name).read_text(), content_type=ctype,
                        headers={"Cache-Control": "no-store, must-revalidate"})


def editor_index(web_root: str) -> web.Response:
    """The engine's index.html with the shared shell injected — the ONE mechanism
    the Editor picks up the Studio/Editor toggle, wallet, GPUs and chat sidebar, the
    same chrome the Studio home includes. Idempotent; no-cache like the upstream
    root handler (the frontend changes every release)."""
    html = Path(web_root, "index.html").read_text()
    if "/studio/shell.js" not in html:
        html = html.replace("</head>", _SHELL_HEAD + "</head>", 1) if "</head>" in html else _SHELL_HEAD + html
        html = html.replace("</body>", _SHELL_BODY + "</body>", 1) if "</body>" in html else html + _SHELL_BODY
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "Expires": "0"})


def _templates_root(org: str) -> Path:
    """orgs/<org>/templates — a per-org sibling of the output tree that holds
    library.json/worklog.json, so templates are tenant-scoped exactly like every
    other per-org file. Created on demand."""
    out = Path(folder_paths.get_org_output_directory(org))
    base = out.parent if out.parent.name == org else out
    d = base / "templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _template_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]


def _template_api_prompt(graph) -> dict | None:
    """The runnable API prompt inside a stored template graph. A graph saved from the
    Editor is ``{workflow, output}`` (output = API prompt); one captured from an
    output PNG is a bare API prompt (numeric ids -> {class_type, inputs}). A UI-only
    graph has neither and can't be queued headless — returns None so the caller says
    so instead of dispatching nothing."""
    if not isinstance(graph, dict) or not graph:
        return None
    out = graph.get("output")
    if isinstance(out, dict) and out:
        return out
    if all(isinstance(v, dict) and "class_type" in v for v in graph.values()):
        return graph
    return None


async def _finance_balance(tok: str) -> dict | None:
    """Raw cloud finance balance for the caller's IAM token, or None on any failure.
    The token scopes the org server-side (cloud pins org from the verified owner
    claim), so this read is inherently tenant-isolated."""
    from middleware.gpu_dispatch import CLOUD
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{CLOUD}/v1/finance/balance", headers={"Authorization": tok}) as r:
                return await r.json() if r.status == 200 else None
    except Exception:
        return None


async def _wallet(request: web.Request, org: str) -> dict:
    """{org, balance, currency} for the wallet chip — a display-only proxy. Balance
    is the spendable dollar amount (availableCents/100); None when signed-out or the
    cloud is unreachable (the chip shows '—', never a fabricated number)."""
    from middleware.gpu_dispatch import _bearer
    out = {"org": org, "balance": None, "currency": None}
    tok = _bearer(request)
    if not tok:
        return out
    b = await _finance_balance(tok)
    if isinstance(b, dict):
        cents = b.get("availableCents")
        if isinstance(cents, (int, float)):
            out["balance"] = round(cents / 100, 2)
        out["currency"] = b.get("currency") or "usd"
    return out


_MAX_UPLOAD = 100 * 1024 * 1024       # 100 MB — 4k masters run 30-60MB; the cap must hold the largest real render
_UPLOAD_EXT = (".png", ".jpg", ".jpeg", ".webp")
_SUBPATH_RE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")


def _safe_subpath(raw: str) -> str | None:
    """The library subfolder an upload lands in — allowlist chars only (no traversal,
    no absolute), default ``renders``. Returns None for anything outside the allowlist
    so the route can reject it."""
    sub = (raw or "").strip("/") or "renders"
    return sub if _SUBPATH_RE.fullmatch(sub) else None


def _row_output(root: Path | None, output_prefix: str | None) -> str | None:
    """The library-relative output for a work-log row: the file a SaveImage prefix
    produced, or — for an INGESTED render whose output_prefix is already the stored
    file's path — that path itself. Lets a mirrored render show its thumbnail and open
    its graph, without a SaveImage-style counter suffix."""
    landed = _landed_output(root, output_prefix)
    if landed is None and root is not None and output_prefix and _safe_asset(root, output_prefix) is not None:
        landed = output_prefix
    return landed


# ── Honesty guard + iteration hygiene ────────────────────────────────────────────
# A >=10-step 1024x1536 Qwen edit takes MINUTES on any real GPU (≈350s on the GB10).
# If an output lands within this many seconds of the job being dispatched, the engine
# did not sample — a graph that errored straight into history, a stale-history reuse,
# a degenerate self-POST on a model-less pod. Such a result is marked FAILED, never
# delivered. The bound is a queued→done wall clock: `ts` (dispatch) and the output's
# mtime are the two timestamps we measure reliably (started_at is a lazy display
# stamp set by the poller, so a real render observed late must never read as fast).
_IMPLAUSIBLE_SECS = 5


def _implausibly_fast(row: dict, finished_at: int) -> bool:
    """True when a completion cannot have sampled for its step count. Sound: a genuine
    >=10-step render's dispatch→landed wall always far exceeds the bound, so a real
    result is never failed; a sub-bound wall proves no sampling ran. steps<10 or an
    unknown step count is exempt (nothing to contradict)."""
    steps = (row.get("params") or {}).get("steps")
    if not isinstance(steps, (int, float)) or steps < 10:
        return False
    ts = row.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return finished_at - ts < _IMPLAUSIBLE_SECS


def _finalize_landed(org: str, root: Path | None, rows: list[dict], now: int) -> None:
    """The ONE place a landed render becomes terminal, shared by the queue poller and
    the work-log view. For each non-terminal row whose output has landed: close it
    `done` at the file mtime — UNLESS it landed implausibly fast, then `failed` with
    "engine did not sample" so a degenerate result never surfaces as a normal one. A
    matched output OLDER than the dispatch is a pre-existing sibling (same prefix,
    earlier fix), not this job's result — left pending until its own output lands.
    Mutates rows in place (mirrors the persisted status)."""
    for r in rows:
        if r.get("status") in ("done", "failed", "cancelled"):
            continue
        landed = _landed_output(root, r.get("output_prefix"))
        if not landed:
            continue
        mt = _output_mtime(root, landed) or now
        if mt < int(r.get("ts", now)):
            continue
        if _implausibly_fast(r, mt):
            worklog.set_status(org, r["id"], "failed",
                               note="completed implausibly fast — engine did not sample")
            r["status"] = "failed"
        else:
            worklog.mark_finished(org, r["id"], mt)
            r["status"], r["finished_at"] = "done", mt


_CORRUPT = ("flagged", "deleted")


def _asset_status(root: Path | None, rel: str) -> str | None:
    """The library curation status of an asset path (draft/approved/flagged/…), or
    None if it isn't in the library."""
    if root is None:
        return None
    for a in _load_library(root).get("assets", []):
        if a.get("path") == rel:
            return a.get("status")
    return None


def _clean_base(org: str, root: Path | None, rel: str) -> tuple[str, bool]:
    """Iteration hygiene, the ONE place the fix base is chosen: if ``rel`` is corrupt
    (quality-gate ``flagged``, or ``deleted``), fix from the NEAREST clean ancestor in
    its lineage instead — so a fix never compounds a known-bad version. Returns
    ``(path_to_fix_from, substituted)``; a clean base is returned unchanged."""
    if root is None or _asset_status(root, rel) not in _CORRUPT:
        return rel, False
    rows = worklog.load(org)
    for r in rows:
        r["output"] = _row_output(root, r.get("output_prefix"))
    ancestors = worklog.build_lineage(rows, rel).get("ancestors") or []
    for anc in reversed(ancestors):  # nearest parent first, walking back to the root
        p = anc.get("path")
        if p and _asset_status(root, p) not in _CORRUPT and _safe_asset(root, p) is not None:
            return p, True
    return rel, False


def _index_upload(org: str, root: Path, rel: str, *, design, kind, role, node) -> bool:
    """Event-driven index at the ingest point so a just-stored render is INSTANTLY
    findable — in Assets (a library.json entry) and in Queue & History (a done
    work-log row carrying its source node) — without waiting for the manifest sweeper.
    Atomic per-org writes like every other metadata file; upserts by path so a
    re-render of the same name never duplicates the asset. Returns True when a NEW
    asset row was created (a newly-surfaced render) — the quality gate's trigger."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lib = _load_library(root)
    assets = lib.setdefault("assets", [])
    ex = next((a for a in assets if a.get("path") == rel), None)
    created = ex is None
    if created:
        assets.append({"path": rel, "design": design, "kind": kind, "role": role,
                       "status": "draft", "tags": [], "updatedAt": now_iso})
    else:
        ex.update(design=design, kind=kind, role=role, updatedAt=now_iso)
    _save_library(root, lib)
    # The worklog upserts by output too: re-ingesting identical bytes (an
    # interrupted mirror, a restored store) must repair a missing row, never
    # stack a second one.
    if not worklog.has_output(org, rel):
        worklog.record(org, kind="render", prompt="", refs=[], uploads=[], parents=[],
                       output_prefix=rel, node=node, status="done",
                       lane=("gpu" if node not in ("upload", "studio pod") else "local"),
                       pid="up-" + uuid.uuid4().hex[:12])
    # If this render was dispatched with a "save outputs to Stack" destination, the
    # landed output joins that Stack now — ALL outputs of a multi-output job do, so
    # membership is assigned at the ONE ingest point (never in the source of truth).
    stacks_store.absorb(org, rel)
    return created


def _org_store(org: str, name: str) -> Path:
    """A per-org metadata file beside worklog.json/library.json (favorites, stacks).
    The same tenant-scoped output tree every per-org file uses; created on demand so
    the store works before the org has a library.json."""
    d = Path(folder_paths.get_org_output_directory(org))
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _load_store(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save_store(p: Path, data) -> None:
    """Atomic tmp+replace, the ONE write pattern for per-org metadata."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def add_studio_home_routes(routes: web.RouteTableDef, server) -> None:

    # Stacks — Procreate-style gallery folders — are their own concern (per-org
    # SQLite, the full REST surface), registered here through injected tenancy
    # resolvers + the library soft-deleter so the module stays decoupled.
    stacks_store.add_stacks_routes(
        routes, server, org_of=_org_of, owner_of=_owner_of,
        workspace_of=_workspace_of, soft_delete_assets=_soft_delete_assets)

    @routes.get("/studio")
    async def studio_home(request: web.Request):
        # no-store: the home HTML changes every release; without this, browsers
        # (and any proxy) heuristically cache it and serve a STALE page after a
        # deploy — which is why UI fixes appeared "not working" to a user still
        # running yesterday's cached page. Always serve fresh; the page is small
        # and its shell.js/shell.css keep their own cache.
        return web.Response(text=_HTML.read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-store, must-revalidate"})

    @routes.get("/studio/shell.js")
    async def shell_js(request: web.Request):
        return _asset("shell.js", "text/javascript")

    @routes.get("/studio/shell.css")
    async def shell_css(request: web.Request):
        return _asset("shell.css", "text/css")

    @routes.get("/v1/workflow")
    async def get_workflow(request: web.Request):
        """The graph that produced a work-log item, for 'Open in Editor'. Tenant-
        scoped: the id must be in THIS org's work log or it 404s (a cross-tenant id
        is never confirmed). The graph is read from the item's output PNG, which
        embeds the litegraph 'workflow' (UI) and/or the API 'prompt'."""
        org = _org_of(request)
        pid = request.query.get("id", "")
        row = next((r for r in worklog.load(org) if r.get("id") == pid), None)
        if row is None:
            return web.json_response({"error": "not found"}, status=404)
        root = _library_root(org)
        landed = _row_output(root, row.get("output_prefix"))
        p = _safe_asset(root, landed) if (root and landed) else None
        if p is None or p.suffix.lower() != ".png":
            return web.json_response({"error": "workflow not available yet"}, status=404)
        try:
            from PIL import Image
            info = Image.open(p).info
        except Exception:
            info = {}
        for key, fmt in (("workflow", "ui"), ("prompt", "api")):
            try:
                g = json.loads(info.get(key, "null"))
            except Exception:
                g = None
            if isinstance(g, dict) and g:
                return web.json_response({"id": pid, "format": fmt, "graph": g})
        return web.json_response({"error": "no embedded workflow in this output"}, status=422)

    @routes.post("/v1/templates")
    async def save_template(request: web.Request):
        """Save a workflow graph as an org template (orgs/<org>/templates/<slug>.json,
        atomic tmp+replace like the work log). Server-resolved org — a template is
        only ever written to, and read from, the caller's own tenant tree."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        name = (body.get("name") or "").strip()
        graph = body.get("graph")
        # A flow template captures a reusable generation FLOW (kind + instruction
        # pattern + references) rather than a node graph — what "favorite → turn into
        # a template" saves for a chat/run. Either a graph or a flow makes a template.
        flow = body.get("flow")
        slug = _template_slug(name)
        if not slug:
            return web.json_response({"error": "name required"}, status=400)
        has_graph = isinstance(graph, dict) and bool(graph)
        has_flow = isinstance(flow, dict) and bool(flow)
        if not has_graph and not has_flow:
            return web.json_response({"error": "graph or flow required"}, status=400)
        rec = {"name": name, "slug": slug, "ts": int(time.time())}
        if has_graph:
            rec["graph"] = graph
        if has_flow:
            rec["flow"] = flow
        d = _templates_root(_org_of(request))
        tmp = d / (slug + ".json.tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(d / (slug + ".json"))
        return web.json_response({"ok": True, "name": name, "slug": slug})

    @routes.get("/v1/templates")
    async def list_templates(request: web.Request):
        org = _org_of(request)
        out = []
        for f in sorted(_templates_root(org).glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except Exception:
                continue
            out.append({"name": rec.get("name") or f.stem, "slug": rec.get("slug") or f.stem,
                        "ts": rec.get("ts", 0),
                        "renderable": _template_api_prompt(rec.get("graph")) is not None})
        return web.json_response({"org": org, "templates": out})

    @routes.post("/v1/templates/render")
    async def render_template(request: web.Request):
        """Queue a saved template through the ONE dispatch funnel (reseeded); its
        output lands in the library via the manifest sidecar — no new path. A
        UI-only graph can't run headless, so it's rejected with a clear reason."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        org = _org_of(request)
        slug = _template_slug(body.get("name", ""))
        f = _templates_root(org) / (slug + ".json")
        if not slug or not f.is_file():
            return web.json_response({"error": "not found"}, status=404)
        try:
            rec = json.loads(f.read_text())
        except Exception:
            return web.json_response({"error": "corrupt template"}, status=422)
        prompt = _template_api_prompt(rec.get("graph"))
        if prompt is None:
            return web.json_response(
                {"error": "this template has no runnable graph — open it in the Editor to run"}, status=422)
        out_prefix = ""
        for n in prompt.values():
            if isinstance(n, dict) and n.get("class_type") == "SaveImage":
                out_prefix = n.get("inputs", {}).get("filename_prefix", "") or out_prefix
        out_prefix = out_prefix or f"templates/{slug}"
        sid = _dest_stack(org, body.get("stack"), prompt=rec.get("name", slug),
                          multi=_graph_is_batch(prompt), owner=_owner_of(request),
                          workspace=_workspace_of(request))
        try:
            pid = await _dispatch(request, server, org, _reseed(json.loads(json.dumps(prompt))),
                                  kind="template", prompt=rec.get("name", slug), refs=[], uploads=[],
                                  parents=[], output_prefix=out_prefix)
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)
        if sid:
            stacks_store.route_prefix(org, out_prefix, sid, added_by=_owner_of(request))
        return web.json_response({"ok": True, "prompt_id": pid, "slug": slug, "stack": sid})

    @routes.get("/v1/templates/{name}")
    async def get_template(request: web.Request):
        org = _org_of(request)
        slug = _template_slug(request.match_info.get("name", ""))
        f = _templates_root(org) / (slug + ".json")
        if not slug or not f.is_file():
            return web.json_response({"error": "not found"}, status=404)
        try:
            return web.json_response(json.loads(f.read_text()))
        except Exception:
            return web.json_response({"error": "corrupt template"}, status=422)

    @routes.get("/v1/wallet")
    async def get_wallet(request: web.Request):
        """The org's wallet balance for the shell chip — a display-only proxy of the
        user's IAM token to the cloud finance balance. Returns {org, balance,
        currency}; top-up is a link to pay.hanzo.ai in the UI. No funds move here."""
        org = _org_of(request)
        return web.json_response(await _wallet(request, org))

    @routes.post("/v1/agent")
    async def agent_bridge(request: web.Request):
        """Same-origin bridge to the cloud agent orchestrator. The session cookie
        stays host-only (never widened to *.hanzo.ai); this forwards the caller's
        bearer server-side — the wallet pattern. Direct-to-gateway remains
        available to the SPA via VITE_AGENT_API."""
        from middleware.gpu_dispatch import CLOUD, _bearer
        tok = _bearer(request)
        if not tok:
            return web.json_response({"error": "auth required"}, status=401)
        body = await request.read()
        if len(body) > 1 << 20:
            return web.json_response({"error": "body too large"}, status=413)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
                async with s.post(f"{CLOUD}/v1/agent", data=body,
                                  headers={"Authorization": tok,
                                           "Content-Type": "application/json"}) as r:
                    return web.Response(status=r.status, body=await r.read(),
                                        content_type="application/json")
        except Exception:
            return web.json_response({"error": "agent unreachable"}, status=502)

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
        return web.json_response({"org": org, "assets": assets, "workflows": _workflows(org)},
                                 headers={"Cache-Control": "no-store"})   # mutable: a delete must never be masked by a cached copy

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

    @routes.post("/v1/library/upload")
    async def library_upload(request: web.Request):
        """Tenant-scoped render ingest so EVERY render lands in the library — including
        ones produced on a GPU node outside the job path (the node's mirror loop POSTs
        here). Body is multipart (field ``image``/``file``) or raw bytes + ``?name=``,
        with an optional ``?subpath=`` (default ``renders``). Writes
        orgs/<org>/output/<subpath>/<name> atomically (tmp+replace, same as the work
        log); rejects traversal (basename-only name + allowlisted subpath); caps size;
        and skips a byte-identical existing file (name+size match → existed). The
        manifest sidecar indexes output/ — no other wiring."""
        org = _org_of(request)
        sub = _safe_subpath(request.query.get("subpath", ""))
        if sub is None:
            return web.json_response({"error": "invalid subpath"}, status=400)
        clen = request.content_length
        if clen is not None and clen > _MAX_UPLOAD:
            return web.json_response({"error": "file too large"}, status=413)
        if request.headers.get("Content-Type", "").startswith("multipart/"):
            post = await request.post()
            f = post.get("image") or post.get("file")
            if f is None or not hasattr(f, "file"):
                return web.json_response({"error": "no image field"}, status=400)
            name = os.path.basename(f.filename or "")
            data = f.file.read()
        else:
            data = await request.read()
            name = os.path.basename(request.query.get("name", ""))
        if not name or os.path.splitext(name)[1].lower() not in _UPLOAD_EXT:
            return web.json_response({"error": "image name required (.png/.jpg/.jpeg/.webp)"}, status=400)
        if name.startswith("."):
            # `._foo.png` (AppleDouble fork) and other dotfiles pass the extension
            # check but are not renders.
            return web.json_response({"error": "hidden file"}, status=400)
        if not data:
            return web.json_response({"error": "empty body"}, status=400)
        if len(data) > _MAX_UPLOAD:
            return web.json_response({"error": "file too large"}, status=413)
        if not (data[:8] == b"\x89PNG\r\n\x1a\n"
                or data[:3] == b"\xff\xd8\xff"
                or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
            # The name lies sometimes; the bytes never do.
            return web.json_response({"error": "not an image"}, status=400)
        out_root = Path(folder_paths.get_org_output_directory(org))
        dest = (out_root / sub / name).resolve()
        if not str(dest).startswith(str(out_root.resolve()) + os.sep):
            return web.json_response({"error": "invalid path"}, status=400)
        rel = f"{sub}/{name}"
        # Identical bytes: skip the write but STILL index — files can exist without
        # rows (an interrupted mirror, a restored store), and the index upsert is
        # idempotent by path. Recovery depends on this.
        existed = dest.is_file() and dest.stat().st_size == len(data)
        if not existed:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
        # index at ingest → instantly findable in Assets + Queue & History (with node)
        created = _index_upload(org, out_root, rel,
                                design=request.query.get("design") or None,
                                kind=request.query.get("kind") or "render",
                                role=request.query.get("role") or None,
                                node=request.query.get("node") or "upload")
        if created:
            # A newly-surfaced render: judge its quality out-of-band (never blocks
            # this response). A clear FAIL flags it out of the default library.
            quality_gate.schedule(request, org, out_root, rel)
        resp = {"ok": True, "path": rel}
        if existed:
            resp["existed"] = True
        return web.json_response(resp)

    @routes.get("/v1/gpu-status")
    async def gpu_status(request: web.Request):
        """Is a GPU worker online for the caller's org? Advisory only — it drives the
        ⚠ warning, never a render decision (dispatch always enqueues to gpu-jobs and
        waits). Reads the fleet with a SERVICE identity so an org whose BYO GPUs are
        registered under a sibling org still sees them; the org-scoped user-token read
        alone reported a false 'no GPU' and drove the whole confusion."""
        try:
            from middleware.gpu_dispatch import _bearer, has_online_gpu_advisory
        except ImportError:
            from .middleware.gpu_dispatch import _bearer, has_online_gpu_advisory
        try:
            online = has_online_gpu_advisory(_bearer(request))
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

    @routes.post("/v1/library/dedup")
    async def dedup(request: web.Request):
        """Soft-delete exact-content duplicates — keep one per byte-identical cluster,
        move the rest to Deleted (recoverable, files kept). Content hash, so only TRUE
        duplicates go; distinct re-renders (different seeds) are untouched. Returns the
        moved paths so the UI can offer a one-shot undo."""
        import hashlib
        org = _org_of(request)
        root = _library_root(org)
        if root is None:
            return web.json_response({"removed": 0, "deleted": []},
                                     headers={"Cache-Control": "no-store"})
        lib = _load_library(root)
        seen: set[str] = set()
        moved = []
        for a in sorted(lib.get("assets", []), key=lambda x: x.get("path", "")):
            if a.get("status") == "deleted":
                continue
            p = _safe_asset(root, a.get("path", ""))
            if p is None:
                continue
            try:
                h = hashlib.md5(p.read_bytes()).hexdigest()
            except OSError:
                continue
            if h in seen:
                moved.append({"path": a["path"], "prev": a.get("status", "draft")})
                a["status"] = "deleted"
                a["updatedAt"] = int(time.time())
            else:
                seen.add(h)
        if moved:
            _save_library(root, lib)
        return web.json_response({"removed": len(moved), "deleted": moved},
                                 headers={"Cache-Control": "no-store"})

    @routes.post("/v1/library/purge")
    async def purge(request: web.Request):
        """Permanently remove ARCHIVED assets — unlink the file AND drop the library
        record. Irreversible: no ⌘Z can bring back an unlinked file, so the UI confirms
        first. Archive-only by design — an asset must already be soft-deleted
        (status='deleted'); a live asset is never hard-deleted directly, so a mis-click
        in the gallery can only ever archive (recoverable), never destroy. Body is
        {paths:[...]} to purge those, or {all:true} to empty the whole Archive."""
        org = _org_of(request)
        root = _library_root(org)
        if root is None:
            return web.json_response({"removed": []}, headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        want_all = bool(body.get("all"))
        ids = {p for p in (body.get("paths") or ([body["path"]] if body.get("path") else []))
               if isinstance(p, str)}
        if not want_all and not ids:
            return web.json_response({"error": "paths or all required"}, status=400)
        lib = _load_library(root)
        kept, removed = [], []
        for a in lib.get("assets", []):
            if a.get("status") == "deleted" and (want_all or a.get("path") in ids):
                p = _safe_asset(root, a.get("path", ""))
                if p is not None:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                _drop_thumb(root, a.get("path", ""))
                removed.append(a.get("path"))
            else:
                kept.append(a)
        if removed:
            lib["assets"] = kept
            _save_library(root, lib)
        return web.json_response({"removed": removed}, headers={"Cache-Control": "no-store"})

    @routes.post("/v1/library/tags")
    async def add_tags(request: web.Request):
        """Merge tags onto a set of library assets (the bulk 'Add tags' action). Tags
        enrich; they never gate. One atomic per-org write, upsert by path."""
        body = await request.json()
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]
        tags = [t.strip()[:40] for t in (body.get("tags") or []) if isinstance(t, str) and t.strip()]
        if not paths or not tags:
            return web.json_response({"error": "paths and tags required"}, status=400)
        root = _library_root(_org_of(request))
        if root is None:
            return web.json_response({"error": "no library for org"}, status=404)
        want = set(paths)
        lib = _load_library(root)
        n = 0
        for a in lib.get("assets", []):
            if a.get("path") in want:
                merged = list(dict.fromkeys((a.get("tags") or []) + tags))
                a["tags"] = merged
                a["updatedAt"] = int(time.time())
                n += 1
        _save_library(root, lib)
        return web.json_response({"ok": True, "updated": n, "tags": tags})

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
        count = max(1, min(int(body.get("count", 1)), 4))
        # Save-outputs-to destination: an explicit Stack, or an auto-stack when the
        # org opted in and this is a batch (count>1). Every rerun shares out_prefix,
        # so one routing rule captures all its landed outputs.
        sid = _dest_stack(org, body.get("stack"), prompt=instr, multi=count > 1,
                          owner=_owner_of(request), workspace=_workspace_of(request))
        ids = []
        try:
            for _ in range(count):
                ids.append(await _dispatch(
                    request, server, org, _reseed(json.loads(json.dumps(graph))),
                    kind="rerun", prompt=instr, refs=[], uploads=[], parents=[rel],
                    output_prefix=out_prefix))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)
        if sid:
            stacks_store.route_prefix(org, out_prefix, sid, added_by=_owner_of(request))
        return web.json_response({"ok": True, "prompt_ids": ids, "stack": sid})

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
                body.get("instruction", ""), body.get("paths"), body.get("uploads"), body.get("stack")))
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
        # finalize any job whose output has landed: done at the file mtime, or FAILED
        # when it landed implausibly fast for its step count (the honesty guard).
        _finalize_landed(org, root, rows, now)
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
        # Same terminal-state mechanism as the queue poller: done at the output mtime,
        # or FAILED when it landed implausibly fast. The view reflects it, never
        # re-derives "done" from a landed file (which would relabel a guarded fail).
        _finalize_landed(org, root, rows, int(time.time()))
        out = []
        for r in reversed(rows):
            row = dict(r)
            row["output"] = _row_output(root, r.get("output_prefix"))
            out.append(row)
        q = (request.query.get("q") or "").lower().strip()
        if q:
            # search prompt + kind + the render's paths (output_prefix/output) + refs, so
            # an ingested render with an empty prompt is still findable by its filename.
            out = [r for r in out if q in " ".join(str(x) for x in
                   [r.get("prompt", ""), r.get("kind", ""), r.get("output_prefix", ""),
                    r.get("output", ""), *(r.get("refs") or [])]).lower()]
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
                body.get("instruction", ""), body.get("upload"), body.get("stack")))
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
                body.get("instruction", ""), body.get("uploads"), body.get("stack")))
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
        if "path" in body:
            # The run's landed output, stored beside the rating so the flywheel row
            # carries {path, stars, note, ts} keyed by run id — the AI can load the
            # image it's being told to do differently next time.
            entry["path"] = str(body.get("path") or "")[:512]
        entry["updatedAt"] = int(time.time())
        data[key] = entry
        tmp = root / "ratings.json.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(rf)
        return web.json_response({"ok": True, "key": key, "entry": entry})

    # ── Favorites: the heart toggle. A per-org set of saved runs/flows, keyed by run
    # id (or output path) exactly like ratings — but a SEPARATE concern: a favorite is
    # the user's bookmark (→ "turn this into a template"), a rating is quality feedback
    # for the AI. One store each; a run can be either, both, or neither. ─────────────
    @routes.get("/v1/favorites")
    async def get_favorites(request: web.Request):
        org = _org_of(request)
        favs = _load_store(_org_store(org, "favorites.json"), {})
        return web.json_response({"org": org, "favorites": favs if isinstance(favs, dict) else {}})

    @routes.post("/v1/favorites")
    async def set_favorite(request: web.Request):
        org = _org_of(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        key = (body.get("key") or "").strip()[:512]
        if not key:
            return web.json_response({"error": "key required"}, status=400)
        fav = bool(body.get("favorite", True))
        p = _org_store(org, "favorites.json")
        data = _load_store(p, {})
        if not isinstance(data, dict):
            data = {}
        if fav:
            data[key] = {"ts": int(time.time())}
        else:
            data.pop(key, None)
        _save_store(p, data)
        return web.json_response({"ok": True, "key": key, "favorite": fav})
