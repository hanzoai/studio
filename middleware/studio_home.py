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
                                 — this workspace's OWN curation state, nothing else
    POST /v1/library/publish     {path}  push it to the Hanzo content lane (cms.hanzo.ai)
                                 as a draft Asset — a DIFFERENT system, hence a different
                                 verb; the upstream status/body comes back verbatim
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

import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

import folder_paths
from middleware import content_publish, quality_gate, stacks_store, worklog

log = logging.getLogger("studio.home")

_HTML = Path(__file__).with_name("studio_home.html")
_ASSET_DIR = Path(__file__).parent
_STATUSES = ("draft", "approved", "queued", "published", "deleted", "flagged")
_PROV_CACHE: dict[str, tuple[float, dict]] = {}  # abspath -> (mtime, provenance)

_NEG = ("ghosting, double exposure, duplicated body, transparent fabric, sheer "
        "fabric, illustration, painterly, blotchy skin, mottled skin, jpeg "
        "artifacts, pixelation, noise, deformed hands, extra limbs, text, watermark, "
        # HOUSE RULE (permanent): CAD/pen construction lines — the black marks a
        # designer draws to indicate creases, ruching, gathers or seams — are
        # sewing guidance, NEVER a visual feature. They must never be rendered on
        # a garment or a model wearing one, in ANY lane.
        "drawn black lines on fabric, pen markings, construction lines, "
        "annotation marks, sketch lines, drawn outlines on clothing")

# The Qwen-Image-Edit-2511 model set, by the EXACT filenames present on the render
# worker (diffusion_models / text_encoders / vae — verified on spark). The graph
# names ONLY these: a graph that referenced a missing checkpoint fails ComfyUI
# validation instantly and lands a degenerate, un-sampled result.
_QWEN_UNET = "qwen-image-edit-2511.safetensors"
_QWEN_CLIP = "qwen2.5-vl-7b.safetensors"
_QWEN_VAE = "qwen-image-vae.safetensors"

# The Wan 2.2 TI2V-5B model set, by the EXACT filenames present on the render worker
# (spark) — the same locality rule as the Qwen set: a graph naming a missing checkpoint
# fails ComfyUI validation instantly. _WAN_NEG is the proven Chinese negative from the
# spark video proof (== zen-video DEFAULT_NEGATIVE): the overbright / static / blurry /
# caption / worst-quality / deformed-limb corruption classes for Wan.
_WAN_UNET = "wan2.2_ti2v_5B_fp16.safetensors"
_WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
_WAN_VAE = "wan2.2_vae.safetensors"
_WAN_NEG = "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"

_VIDEO_EXT = (".mp4", ".webm")
_MODEL3D_EXT = (".glb",)                  # Hunyuan3D SaveGLB output
_HY3D_CKPT = "hunyuan_3d_v2.1.safetensors"  # bundled model+CLIP-vision+VAE (Comfy-Org repackaged)

# The Z-Image-Turbo text-to-image model set, by the EXACT filenames the shipped
# "Text to Image (Z-Image-Turbo)" blueprint names (unet / lumina2 text encoder / vae) —
# the same locality rule as the Qwen/Wan sets. Turbo → 4 steps, cfg 1.0. This is the
# composer's default "What should we create?" lane: words in, an image out.
_ZIMAGE_UNET = "z_image_turbo_bf16.safetensors"
_ZIMAGE_CLIP = "qwen_3_4b.safetensors"
_ZIMAGE_VAE = "ae.safetensors"
_SAVE_EXT = (".png",) + _VIDEO_EXT + _MODEL3D_EXT   # every extension a saver writes
try:
    from middleware.gpu_dispatch import SAVE_NODES as _SAVERS   # the ONE output-node list
except ImportError:  # alternate package anchor (same fallback the lazy gpu_dispatch imports use)
    from .middleware.gpu_dispatch import SAVE_NODES as _SAVERS


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


def _embedded(path: Path) -> dict:
    """The metadata a saver embedded in an output, for BOTH media, as a {key: raw-json}
    mapping: a PNG carries it in PIL image ``info`` (SaveImage), an mp4/webm carries it in
    the CONTAINER metadata (SaveVideo/SaveWEBM write the SAME JSON strings PIL returns).
    Returns {} when absent/unreadable. The ONE reader — provenance, Open-in-Editor and
    re-run all parse the ``prompt`` (API graph) out of it (Open-in-Editor also reads the UI
    ``workflow``), so video never silently loses its Versions / Re-run / lessons surface."""
    suf = path.suffix.lower()
    try:
        if suf == ".png":
            from PIL import Image
            return dict(Image.open(path).info)
        if suf in _VIDEO_EXT:
            import av
            with av.open(str(path)) as c:
                return dict(c.metadata)
    except Exception:  # non-Comfy media, truncated file — metadata is optional
        pass
    return {}


def _poster(p: Path):
    """The first video frame as a PIL image — the card poster. PyAV in-process (the studio
    image ships no ffmpeg binary, and SaveVideo already encodes with av, so av is present).
    Raises on any decode failure; the caller turns that into a 404, never the whole file."""
    import av
    with av.open(str(p)) as c:
        frame = next(c.decode(video=0))
        return frame.to_image()


def _asset_graph(abs_path: Path) -> dict:
    """The API graph the saver embedded in an output — the reproducibility record.
    The ONE source both the provenance panel and the CMS publish read; {} when the
    file carries none (a hand-uploaded reference, a truncated write)."""
    try:
        g = json.loads(_embedded(abs_path).get("prompt", "null"))
    except Exception:  # non-Comfy output, truncated file — provenance is optional
        return {}
    return g if isinstance(g, dict) else {}


def _provenance(abs_path: Path) -> dict:
    """What made this output, distilled from its embedded graph — PNG or video.
    ONE distiller (``_graph_params``, shared with the dispatch record) plus the two
    facts only a finished artifact carries: the saver's prefix and the model."""
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        return {}
    key = str(abs_path)
    hit = _PROV_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    graph = _asset_graph(abs_path)
    prov = _graph_params(graph)
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") in _SAVERS:
            prov["workflow"] = (node.get("inputs") or {}).get("filename_prefix", "")
    if graph:
        prov["model"] = content_publish.model_of(graph)
        prov["nodes"] = len(graph)
    _PROV_CACHE[key] = (mtime, prov)
    return prov


def _norm_vote(v) -> int:
    """Any vote input → the canonical preference label: 1 (👍), -1 (👎), 0 (cleared)."""
    if isinstance(v, str):
        return {"up": 1, "down": -1, "1": 1, "-1": -1}.get(v.strip().lower(), 0)
    if isinstance(v, (int, float)):
        return 1 if v > 0 else -1 if v < 0 else 0
    return 0


def _append_feedback(request, root: Path, org: str, key: str, entry: dict) -> None:
    """Append ONE immutable training-feedback event to orgs/<org>/output/feedback.jsonl —
    the flywheel dataset the model trainer consumes. ratings.json is the CURRENT-state
    store the UI reads; THIS is the append-only log of every judgement (vote + stars +
    text) paired with the output it judged, so preference / reward / instruction-tuning
    signal is built downstream (the output PNG carries its own prompt + refs in its
    embedded graph, so the record only needs to anchor to it). Best-effort — never
    blocks or fails the rating write."""
    try:
        path = entry.get("path") or (key if key.lower().endswith(_REF_EXT) else "")
        rec = {"ts": int(time.time()), "user": _owner_of(request), "org": org, "key": key,
               "output": path, "vote": entry.get("vote"), "stars": entry.get("stars"),
               "text": entry.get("notes") or ""}
        # The output's curation state rides along: a flagged/deleted output is
        # negative-only signal downstream — never a positive training exemplar.
        if path and root is not None:
            rec["status"] = _asset_status(root, path)
        if path:
            p = _safe_asset(root, path)
            if p is not None:
                rec["prov"] = _provenance(p)
        with open(root / "feedback.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _rate_write(root: Path, key: str, patch: dict) -> dict:
    """The ONE atomic writer for the current-state rating store (ratings.json) — the
    append-only training log is _append_feedback. Merges ``patch`` into the entry and
    replaces the file via a UNIQUE temp, so two concurrent writers (a manual rate and an
    archive hook) can never clobber each other on a shared temp name — the hazard already
    fixed for library.json."""
    rf = root / "ratings.json"
    try:
        data = json.loads(rf.read_text())
    except Exception:
        data = {}
    entry = {**data.get(key, {}), **patch, "updatedAt": int(time.time())}
    data[key] = entry
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".ratings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, rf)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return entry


def _train_on_status(request, root: Path, org: str, rel: str, status: str) -> None:
    """Curation IS training signal: archiving or flagging an output records it as a
    NEGATIVE example — the user threw it away, so it is a bad response — and restoring it
    clears that auto-negative. The vote is NOTE-LESS on purpose: it trains the
    reward/preference dataset (feedback.jsonl, where the deleted/flagged status rides
    along as negative-only signal) WITHOUT injecting a generic 'lesson' into future
    prompts (_mistakes gates on note text, so a bare downvote never pollutes a prompt).
    A judgement the user typed themselves (a manual vote/note) is respected — never
    overwritten by, or cleared with, the automatic archive-negative. Automatic dedup does
    NOT pass through here, so byte-identical twins are never mislabelled bad."""
    try:
        try:
            cur = json.loads((root / "ratings.json").read_text()).get(rel, {})
        except Exception:
            cur = {}
        user_owned = bool(cur.get("notes")) or ("vote" in cur and not cur.get("archived"))
        if status in _CORRUPT:                       # archived / flagged → negative exemplar
            if not user_owned:
                cur = _rate_write(root, rel, {"vote": -1, "archived": True, "path": rel})
            _append_feedback(request, root, org, rel, {**cur, "path": rel})
        elif cur.get("archived"):                    # restored → clear ONLY our auto-negative
            cur = _rate_write(root, rel, {"vote": 0, "archived": False, "path": rel})
            _append_feedback(request, root, org, rel, {**cur, "path": rel})
    except Exception:
        log.debug("train-on-status skipped for %s", rel, exc_info=True)


try:
    import fcntl as _fcntl
except ImportError:  # non-POSIX; never the studio image
    _fcntl = None


def _load_library(root: Path) -> dict:
    try:
        return json.loads((root / "library.json").read_text())
    except Exception:
        return {"assets": []}


def _save_library(root: Path, lib: dict) -> None:
    """Write library.json atomically AND serialized against the build-manifest
    sidecar. Two failure modes this closes: (1) a fixed shared tmp let two writers
    interleave into one inode -> torn JSON -> reader gets {} -> every asset resets to
    draft (the curation wipe) — mkstemp gives each writer a private tmp; (2) the
    sidecar's rebuild-then-replace and this write could interleave renames — a shared
    flock (.library.lock, the same file scripts/library_manifest.py takes across its
    build+write) serializes them so neither clobbers the other's file operation."""
    lock_path = root / ".library.lock"
    lh = open(lock_path, "w") if _fcntl is not None else None
    try:
        if lh is not None:
            _fcntl.flock(lh, _fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(prefix="library.json.", suffix=".tmp", dir=str(root))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(lib, fh, indent=1, sort_keys=False)
            os.replace(tmp, str(root / "library.json"))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        if lh is not None:
            with contextlib.suppress(OSError):
                _fcntl.flock(lh, _fcntl.LOCK_UN)
            lh.close()


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
        for ext in (".webp", ".jpg"):   # current WebP thumb + any legacy JPEG
            (root / ".thumbs" / f"{key}{ext}").unlink(missing_ok=True)
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


def _retag(graph: dict, prefix: str) -> dict:
    """Point every saver (SaveImage / SaveVideo / SaveWEBM) in a re-run / template graph
    at a UNIQUE output prefix. Without this a re-run replays the source graph's own saver
    prefix, so its file lands in the SOURCE's namespace and N re-runs share one prefix —
    the library then attributes the newest sibling to every row (a job shows a photo it
    did not make). fix/compose/amend already tag per dispatch; this gives re-run and
    template-render the same job-identity guarantee, for image AND video outputs."""
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") in _SAVERS:
            node.setdefault("inputs", {})["filename_prefix"] = prefix
    return graph


def _mistakes(ratings: dict, family) -> list[str]:
    """The correction texts inside ``ratings`` for any asset in ``family`` — a note
    counts as a MISTAKE when its judgement is negative (👎 or ≤2 stars) or carries no
    judgement at all (a bare note points something out); praise notes are not
    avoid-clauses. Deduped, sanitized, capped for prompt injection."""
    out: list[str] = []
    for path in family:
        r = ratings.get(path) if path else None
        if not isinstance(r, dict):
            continue
        note = " ".join((r.get("notes") or "").split()).strip()[:140]
        if not note:
            continue
        vote, stars = r.get("vote"), r.get("stars")
        if vote == -1 or (isinstance(stars, int) and stars <= 2) or (vote in (None, 0) and stars is None):
            if note not in out:
                out.append(note)
    return out[:4]


def _source_size(path: Path) -> tuple[int, int] | None:
    """The source's own pixel dimensions, or None when they cannot be read.

    The edit samples at the model's resolution whatever this says; this is only what
    the OUTPUT is restored to, so an unreadable file costs the master's size and never
    the render. None means "behave exactly as before".
    """
    try:
        from PIL import Image  # local, like every other reader here
        with Image.open(path) as im:
            w, h = im.size
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _lessons(org: str, root: Path | None, rels: list[str]) -> list[str]:
    """Every mistake the user has pointed out on these assets or their lineage —
    read at DISPATCH so the very next generation is conditioned against repeating
    it. The same judgements also accumulate in feedback.jsonl for weight training."""
    rels = [r for r in rels if r]
    if root is None or not rels:
        return []
    try:
        ratings = json.loads((root / "ratings.json").read_text())
    except Exception:
        return []
    if not ratings:
        return []
    rows = worklog.load(org)
    _resolve_outputs(org, root, rows, int(time.time()))
    family = set(rels)
    for rel in rels:
        lin = worklog.build_lineage(rows, rel)
        family |= {a.get("path") for a in lin.get("ancestors") or []}
        family |= {d.get("path") for d in lin.get("descendants") or []}
    return _mistakes(ratings, family)


def _job_tag() -> str:
    """Six hex chars appended to every saver prefix so each dispatch owns a
    UNIQUE output namespace — a re-fix of the same source can never prefix-match an
    earlier run's (possibly rejected) file as its own result."""
    return uuid.uuid4().hex[:6]


def _stage_source(in_dir: Path, p: Path, kind: str, *, max_px: int | None = None) -> str:
    """Stage a library asset into the org input dir under a CONTENT-ADDRESSED name — the
    ONE way an Edit/Compose/Video/3D source reaches the worker's LoadImage. The name is
    the sha256 of the source bytes, which makes the source binding self-verifying and
    concurrency-safe:
      * the name IS a checksum of the exact selected image, so the bytes the worker loads
        can never silently drift to another asset (a mismatch would be a different name);
      * two jobs editing DIFFERENT sources can never collide on a shared worker input dir
        (different bytes -> different name, cryptographically) — the cross-contamination
        that a process-salted hash could, rarely, produce for concurrent edits;
      * two jobs on the SAME source share one identical file (idempotent, dedup-friendly).
    ``max_px`` downsizes the STAGED copy (Hunyuan3D encodes small; keeps the enqueue under
    the inline cap) — the content-address still keys on the ORIGINAL asset, so distinct
    sources stay distinct regardless of the resample."""
    data = p.read_bytes()
    ref_name = f"{kind}_src_{hashlib.sha256(data).hexdigest()[:16]}.png"
    dst = in_dir / ref_name
    if max_px:
        try:
            from PIL import Image
            im = Image.open(p)
            if max(im.size) > max_px:
                im.thumbnail((max_px, max_px))
                im.save(dst)
                return ref_name
        except Exception:
            pass
    dst.write_bytes(data)
    return ref_name


def _trace(stage: str, **fields) -> None:
    """One structured render-lifecycle line, correlated by job id, at each hop
    (build -> queue -> worker-publish) so a wrong asset/prompt is pinpointed to the
    FIRST stage it appears. NEVER logs signed URLs, tokens, cookies or image bytes —
    only ids, content-address checksums, counts and lane/node labels."""
    kv = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    log.info("studio.trace %s %s", stage, kv)


_TAG_RE = re.compile(r"_[0-9a-f]{6}$")


def _stamp(prefix: str) -> str:
    """Give an output prefix a FRESH job identity: strip a trailing job tag from its LAST
    path segment if present (so a re-run of a re-run, or a re-render of a saved template,
    does not accumulate tags), then append a new one. Every dispatch of a STORED graph —
    template render, re-run — thus owns a unique output namespace (the house job rule)."""
    head, sep, tail = prefix.rpartition("/")
    tail = _TAG_RE.sub("", tail) + f"_{_job_tag()}"
    return f"{head}/{tail}" if sep else tail


# View/pose-change intent: these instructions need the empty-latent reference
# family — the anchored fix latent (VAEEncode of the base) preserves composition
# by design, which is exactly wrong when the user asks for a different view.
_REPOSE = re.compile(
    r"\b(from the (side|back|front|left|right)|side view|back view|front view|profile|"
    r"three.?quarter|turn(ed|s|ing)? (around|left|right)|facing (left|right|away)|"
    r"different (pose|angle|view)|change (the )?(pose|angle|view)|from behind|"
    r"standing|sitting|kneeling|walking|lying|looking (left|right|away|back|over))\b", re.I)


def _repose_size(p: Path) -> tuple[int, int]:
    """EmptySD3LatentImage dims for a view/pose change: the reference's aspect at
    ~1MP, both sides multiples of 16 (latent stride)."""
    try:
        from PIL import Image
        w, h = Image.open(p).size
    except Exception:
        return 1024, 1536
    scale = (1_048_576 / max(1, w * h)) ** 0.5
    def to16(v: float) -> int:
        return max(256, min(2048, int(round(v * scale / 16)) * 16))
    return to16(w), to16(h)


# Edit-strength → denoise. The Edit action is a LOW-denoise img2img over the SOURCE
# latent — never EmptyLatentImage, never denoise 1.0 (that is Regenerate). 0.25 default
# preserves composition/palette/lighting/subject and applies only the requested change;
# the slider lets a bigger edit dial up. Clamped to the edit band [0.05, 0.95] — a true
# 1.0 is the separate Regenerate action, so an Edit can NEVER silently become a regen.
EDIT_DENOISE_DEFAULT = 0.25
EDIT_DENOISE_MIN, EDIT_DENOISE_MAX = 0.05, 0.95
# Where an EDIT stops and a CONVERSION begins. It is not the slider's range above —
# that is only what a caller may ask for. This is the line the lanes already sit either
# side of: style lanes ride the 0.25 default and a garment swap 0.45, while a
# drawing→photograph conversion needs 0.85+ (flows_test pins both directions). It
# decides whether the master's own pixels are still the subject, so it is stated once
# and read wherever that question is asked.
EDIT_BAND_MAX = 0.5


# The instruction itself declares the transformation distance when no lane was picked.
# "make this into a CAD drawing" is a CONVERSION — at the 0.25 edit default it comes back
# VISIBLY UNCHANGED, which is the mirror of the plaid failure: too little change is as
# wrong as too much. These are EXPLICIT rewrite phrasings only ("make/turn this into",
# "convert to", "as a photograph"); an ordinary edit ("remove the lines", "brighten",
# "make the straps thinner") must never match, so nothing is silently re-rendered.
_CONVERSION_PHRASES = (
    "make this into", "make it into", "turn this into", "turn it into",
    "convert this", "convert it", "convert to", "redraw",
    "as a cad", "into a cad", "to a cad",
    "as a photo", "into a photo", "to a photo",
    "as a real", "into a real", "to a real",
    "photorealistic version", "real life version", "real-life version",
)
CONVERSION_DENOISE = 0.90


def asks_conversion(instruction: str) -> bool:
    """True when the instruction EXPLICITLY asks to remake the image as something else
    (a drawing→photograph or photograph→drawing rewrite) rather than to edit it."""
    t = (instruction or "").lower()
    return any(p in t for p in _CONVERSION_PHRASES)


def _edit_denoise(strength, default: float = EDIT_DENOISE_DEFAULT) -> float:
    """Map an edit-strength input to a KSampler denoise in the edit band. Accepts a
    fraction (0.25) or a percent (25). None/invalid → ``default`` (the flow's lane
    default when a template lane is active, else the 0.25 edit default)."""
    try:
        v = float(strength)
    except (TypeError, ValueError):
        return max(EDIT_DENOISE_MIN, min(EDIT_DENOISE_MAX, float(default)))
    if v > 1.0:                      # a percent (25) → fraction
        v = v / 100.0
    return max(EDIT_DENOISE_MIN, min(EDIT_DENOISE_MAX, v))


# ── Per-KIND flow policy — the ONE place a picked template lane shapes a render.
# The composer's template cards send their `kind` with the dispatch body; each
# entry declares how the user's words are FRAMED (the sentence appended after the
# instruction, REPLACING the generic edit-preservation clause), what is negatively
# excluded, and the edit denoise that lane needs — a pen/CAD drawing must be
# nearly fully re-rendered to become a photograph, a photo edit stays light.
# No entry (kind absent/unknown) ⇒ the user's words ride unshaped: a hidden style
# can never leak into a run the user didn't ask to style. An explicit strength
# from the caller still beats the lane's denoise.
# Per-lane render policy. `frame` shapes the positive prompt, `negative` extends the
# global negative, and `denoise` is declared by a lane ONLY when that lane is a
# CONVERSION — because denoise follows the TRANSFORMATION DISTANCE, never the output
# style:
#
#   drawing → photograph  : a large change is REQUIRED, so cad_* run near-full (0.92).
#   photograph → edited photograph : the print, the model's identity, the framing and
#     the resolution must SURVIVE, so a style lane keeps the 0.25 edit default.
#
# Braiding the two cost a real render: `product` shipped at 0.90 and an ordinary
# "remove the drawn lines" edit came back with the watercolor print replaced by plaid,
# a different model, and a lower-resolution frame — the "it says it fixed it but it
# destroyed my design" failure. A lane that only names a LOOK must not license a
# re-render; `outfit` is the one non-conversion lane that legitimately replaces a large
# region (the garment) and it stays preservation-biased.
FLOWS: dict[str, dict] = {
    "product": {
        "frame": ("Professional e-commerce product photograph: plain seamless studio "
                  "background, soft even lighting, accurate colors, true fabric texture, "
                  "sharp focus on the product. No scene, no props, no people unless "
                  "explicitly requested."),
        "negative": ("sketch, line art, drawing, illustration, lifestyle scene, "
                     "environment, props, clutter"),
        # No denoise: see the note above — a style lane edits a photograph.
    },
    "lifestyle": {
        "frame": ("Editorial lifestyle photograph: the product in a real scene in natural "
                  "use, photorealistic, natural light."),
        "negative": "sketch, line art, drawing, illustration",
        # No denoise: a style lane edits a photograph (see the note above).
    },
    "cad_ghost": {
        "frame": ("Convert this garment technical drawing into a PHOTOREALISTIC "
                  "ghost-mannequin e-commerce photograph: real fabric on an invisible "
                  "form, plain seamless background. Interpret pen and annotation marks "
                  "as sewing construction — put creases, ruching, gathers, seams and "
                  "fabric changes exactly where they are indicated, rendered as real "
                  "fabric behavior — and never draw the marks, arrows or text "
                  "themselves. Keep the garment's exact shape, proportions, colorway "
                  "and panel layout from the drawing."),
        "negative": ("sketch, line art, drawing, pen strokes, annotation marks, arrows, "
                     "text, flat illustration, paper, visible mannequin, model, person"),
        "denoise": 0.92,
    },
    "cad_mockup": {
        "frame": ("Convert this technical drawing into a PHOTOREALISTIC mockup "
                  "photograph — the garment worn by a real model, or the packaging as a "
                  "real object, as instructed. Real materials, real light. Interpret pen "
                  "and annotation marks as construction — creases, ruching, gathers, "
                  "seams and fabric changes exactly where indicated, as real fabric "
                  "behavior — and never draw the marks, arrows or text themselves. Keep "
                  "the exact shape, proportions and colorway from the drawing."),
        "negative": ("sketch, line art, drawing, pen strokes, annotation marks, arrows, "
                     "text, flat illustration, paper"),
        "denoise": 0.92,
    },
    "outfit": {
        "frame": ("Swap the garment as instructed while keeping the SAME model, pose, "
                  "framing, lighting and background. Photorealistic, true fabric "
                  "texture."),
        "negative": ("sketch, drawing, illustration, different model, changed pose, "
                     "changed background"),
        # A garment swap replaces a large region, but the frame promises the SAME model,
        # pose and background — so it stays well below the conversion band.
        "denoise": 0.45,
    },
}


def _flow_of(kind) -> dict | None:
    """Resolve a dispatch body's `kind` to its flow policy. Unknown/absent kinds are
    None — the catalog has non-render cards (music, deck, …) whose kind may ride
    along; they must never error a render, only never shape it."""
    return FLOWS.get(str(kind or "").strip().lower()) or None


def _fix_graph(ref_name: str, instruction: str, prefix: str, *, flow: dict | None = None,
               denoise: float = EDIT_DENOISE_DEFAULT,
               seed: int | None = None, lessons: list[str] | None = None,
               size: tuple[int, int] | None = None) -> dict:
    """LOW-DENOISE Qwen-Image-Edit-2511 instruction edit — the EDIT action.

    The source is LoadImage'd, scaled ONCE (FluxKontextImageScale), and fed to BOTH the
    edit conditioning (TextEncodeQwenImageEditPlus) AND the sampling latent (VAEEncode) —
    so KSampler starts from the SOURCE latent, NEVER an empty one. ``denoise`` (default
    0.25, from the edit-strength slider) is how much may change: at 0.25 the source
    latent dominates → composition, palette, lighting, framing and subject are preserved
    and only the requested change is applied. ``seed`` reuses the source's original seed
    by default (stable iteration; a locked seed + identical input + settings is
    deterministic); a caller may vary it and STILL edit the source (the latent is the
    KSampler input regardless of seed). AuraFlow shift + CFGNorm are the Qwen recipe.

    INVARIANTS (see _assert_edit_graph): latent_image is the VAEEncode of the source (no
    EmptyLatentImage), and denoise is in the edit band (never 1.0). A pose/view change or
    a fresh interpretation is REGENERATE (a separate action), not this."""
    # A flow lane's frame REPLACES the generic preservation clause: converting a
    # pen/CAD drawing to a photograph is the OPPOSITE of "keep the same materials,
    # colors and background", and keeping that clause is exactly what made CAD
    # conversions come out still looking like drawings.
    shape = (flow or {}).get("frame") or (
        "Photorealistic; keep the same subject, composition, pose, framing, "
        "proportions, lighting, materials, colors and background — apply ONLY the "
        "requested change, leave everything else unchanged.")
    prompt = f"{instruction.strip()}. {shape}"
    neg = _NEG + (", " + flow["negative"] if flow and flow.get("negative") else "")
    # Lessons steer through the NEGATIVE only. Injecting them into the positive
    # ("do not repeat: lifestyle scene") puts the offending words INTO the
    # conditioning — diffusion models weigh tokens, not negation — which is how a
    # critique note about lifestyle drift was RE-CAUSING lifestyle drift.
    if lessons:
        neg = neg + ", " + ", ".join(lessons)
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "2": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": _QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": _QWEN_UNET, "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _QWEN_VAE}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": prompt}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": neg}},
        "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["5", 0]}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "12": {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}},
        "13": {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0],
                 "seed": seed if seed is not None else random.randrange(2**31), "steps": 30, "cfg": 4.0,
                 "sampler_name": "euler", "scheduler": "simple", "denoise": float(denoise)}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}},
        # The edit runs at the model's own resolution; the OUTPUT keeps the master's.
        #
        # FluxKontextImageScale (node 2) rescales any input to a Kontext bucket of
        # about a megapixel, because that is where Qwen-Image-Edit works. That is a
        # property of the model and not a setting — but it silently became a property
        # of the DELIVERABLE too: a 4096x6144 master (25.2MP) came back 832x1248
        # (1.04MP), a twenty-fourth of the pixels the photographer shot. "Brighten
        # very slightly" is not a request to resample someone's master down.
        #
        # So when the caller says what the source measured, the decode is scaled back
        # to exactly that. It does not recover detail the sampler never saw — nothing
        # can, short of tiling — but it returns a file at the size the rest of the
        # pipeline (print, catalog, the gallery's own full-res view) expects, instead
        # of one that has to be explained. lanczos because this is a photographic
        # upscale and it is the sharpest of the resamplers here without ringing.
        #
        # Absent a size, the graph is exactly what it was: the bucket is the output.
        **(_restore_master(size, denoise) if size else {}),
        "15": {"class_type": "SaveImage",
               "inputs": {"images": [_output_node(size, denoise), 0], "filename_prefix": prefix}},
    }


# Which node holds the deliverable — the decode, the resize, or the composite.
def _output_node(size, denoise) -> str:
    if not size:
        return "14"
    return "21" if _composites(denoise) else "16"


# WHEN the master's own pixels are the better answer.
#
# denoise already says how far the image must travel (see FLOWS): an EDIT adjusts what
# was photographed, a CONVERSION replaces it. That distinction decides this too. Under
# the edit band the original is still the subject and its detail is the thing being
# preserved; at conversion strength the model's output IS the deliverable and
# compositing would fight the conversion it was asked for.
def _composites(denoise: float) -> bool:
    return float(denoise) <= EDIT_BAND_MAX


def _restore_master(size, denoise) -> dict:
    """Return the master at full resolution with the edit's change applied to it.

    RESIZING THE EDIT IS NOT RESTORING THE MASTER, and the numbers say so plainly. The
    model works at a Kontext bucket of about a megapixel; scaling that back to
    4096x6144 gives a file of the right SIZE whose detail is gone — measured against a
    real render: high-frequency energy 10.3 in the camera original, 0.6 in the resized
    edit, and 17.2MB of photograph arriving as 3.0MB of soft. The fabric weave and skin
    texture were not recovered by making the picture bigger.

    So the deliverable is built the other way round: keep every pixel the camera took,
    and add only what the model CHANGED.

        result = original + (edit_upscaled - original_at_the_model's_own_resolution)

    The bracketed term is exactly the low-frequency change — tone, colour, the
    adjustment that was asked for — because both sides passed through the same bucket.
    Everything the model could not see, the original still has. Measured on the same
    render: detail 9.9 against the master's 10.3, with the edit's brightness carried
    through. 96% of the photograph, and the change.

    ImageAddMulti clamps at zero, so the signed difference is taken as two one-sided
    halves and applied in turn. pos and neg are disjoint by construction — where one
    is non-zero the other is zero — so `original + pos - neg` reconstructs the signed
    delta exactly, clipping only where a highlight genuinely reaches white.

    NOT an upscaler. 4x-UltraSharp is installed and would look sharper immediately, but
    it INVENTS texture, and a product photograph that shows fabric the garment does not
    have is worse than a soft one. Nothing here synthesises detail; it only declines to
    throw the original's away.
    """
    w, h = int(size[0]), int(size[1])
    full = {"upscale_method": "lanczos", "width": w, "height": h, "crop": "disabled"}
    g = {
        # The edit, at the master's dimensions.
        "16": {"class_type": "ImageScale", "inputs": {"image": ["14", 0], **full}},
    }
    if not _composites(denoise):
        return g
    g.update({
        # The original as the MODEL saw it: through the same bucket, back up the same
        # way, so the difference below is only what the model did — never resampling.
        "17": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], **full}},
        # The signed change, as two non-negative halves.
        "18": {"class_type": "ImageAddMulti", "inputs": {
            "inputcount": 2, "image_1": ["16", 0], "image_2": ["17", 0],
            "blending": "subtract", "blend_amount": 1.0}},
        "19": {"class_type": "ImageAddMulti", "inputs": {
            "inputcount": 2, "image_1": ["17", 0], "image_2": ["16", 0],
            "blending": "subtract", "blend_amount": 1.0}},
        # Applied to the full-resolution original, which supplies every real pixel.
        "20": {"class_type": "ImageAddMulti", "inputs": {
            "inputcount": 2, "image_1": ["1", 0], "image_2": ["18", 0],
            "blending": "add", "blend_amount": 1.0}},
        "21": {"class_type": "ImageAddMulti", "inputs": {
            "inputcount": 2, "image_1": ["20", 0], "image_2": ["19", 0],
            "blending": "subtract", "blend_amount": 1.0}},
    })
    return g


def _inpaint_graph(ref_name: str, mask_name: str, instruction: str, prefix: str, *,
                   flow: dict | None = None,
                   denoise: float = EDIT_DENOISE_DEFAULT, seed: int | None = None,
                   lessons: list[str] | None = None) -> dict:
    """MASKED Qwen-Image-Edit — the EDIT-SELECTED-AREA action. Same low-denoise edit as
    _fix_graph, but SetLatentNoiseMask restricts sampling to the painted region, and
    ImageCompositeMasked composites the decode back over the ORIGINAL so pixels OUTSIDE
    the mask stay byte-identical. The mask is a user-painted image (white = edit here),
    scaled to the source's scaled size so latent + mask align."""
    shape = (flow or {}).get("frame") or (
        "Photorealistic; change ONLY the masked region to apply the requested edit "
        "and blend it seamlessly; keep the subject, lighting, materials and colors "
        "consistent with the rest of the image.")
    prompt = f"{instruction.strip()}. {shape}"
    neg = _NEG + (", " + flow["negative"] if flow and flow.get("negative") else "")
    if lessons:
        neg = neg + ", " + ", ".join(lessons)  # negative-only (see _fix_graph)
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "2": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": _QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": _QWEN_UNET, "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _QWEN_VAE}},
        # Mask: load its alpha/luma, resize to the scaled source so the latent noise-mask aligns.
        "8": {"class_type": "LoadImage", "inputs": {"image": mask_name}},
        "9": {"class_type": "ImageToMask", "inputs": {"image": ["8", 0], "channel": "red"}},
        "16": {"class_type": "ResizeMask", "inputs": {"mask": ["9", 0], "width": ["2", 0], "height": ["2", 0]}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": prompt}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["3", 0], "vae": ["5", 0], "image1": ["2", 0], "prompt": neg}},
        "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["5", 0]}},
        "17": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["10", 0], "mask": ["16", 0]}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}},
        "12": {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}},
        "13": {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["17", 0],
                 "seed": seed if seed is not None else random.randrange(2**31), "steps": 30, "cfg": 4.0,
                 "sampler_name": "euler", "scheduler": "simple", "denoise": float(denoise)}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}},
        # Composite the edited decode over the ORIGINAL scaled source, masked — unmasked
        # pixels are the source's own, so only the painted region ever changes.
        "18": {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["2", 0], "source": ["14", 0], "mask": ["16", 0], "x": 0, "y": 0, "resize_source": False}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["18", 0], "filename_prefix": prefix}},
    }


# Edit-graph invariants — validated before every Edit dispatch so an Edit can NEVER
# silently become a text-to-image Regenerate. Returns "" if valid, else the reason.
def _positive_encoders(graph: dict) -> set:
    """The node ids the samplers condition POSITIVELY on — the ONLY safe way to
    tell a positive encoder from a negative one. Comparing encoder text against
    _NEG breaks the moment lessons (or a flow) augment the negative string."""
    pos: set = set()
    for n in graph.values():
        if isinstance(n, dict) and "KSampler" in n.get("class_type", ""):
            ref = n.get("inputs", {}).get("positive")
            if isinstance(ref, list) and ref:
                pos.add(str(ref[0]))
    return pos


def _assert_edit_graph(graph: dict) -> str:
    ks = next((n for n in graph.values() if isinstance(n, dict) and n.get("class_type") == "KSampler"), None)
    if not ks:
        return "edit graph has no KSampler"
    ins = ks.get("inputs", {})
    lat_ref = ins.get("latent_image")
    lat_node = graph.get(lat_ref[0]) if isinstance(lat_ref, list) and lat_ref else None
    lat_ct = (lat_node or {}).get("class_type", "")
    if "Empty" in lat_ct:
        return "edit KSampler.latent_image is an EmptyLatentImage (that is Regenerate, not Edit)"
    if lat_ct not in ("VAEEncode", "SetLatentNoiseMask"):
        return f"edit latent must be VAEEncode/SetLatentNoiseMask of the source, not {lat_ct or 'nothing'}"
    dn = ins.get("denoise", 1.0)
    if not (EDIT_DENOISE_MIN <= float(dn) <= EDIT_DENOISE_MAX):
        return f"edit denoise {dn} is outside the edit band [{EDIT_DENOISE_MIN},{EDIT_DENOISE_MAX}] (1.0 = Regenerate)"
    if not any(isinstance(n, dict) and n.get("class_type") == "VAEEncode" for n in graph.values()):
        return "edit graph is missing VAEEncode of the source"
    if not any(isinstance(n, dict) and n.get("class_type") in _SAVERS for n in graph.values()):
        return "edit graph does not save an output"
    return ""


def _video_graph(prompt: str, prefix: str, *, ref_name: str | None = None,
                 width: int = 640, height: int = 640, length: int = 73,
                 seed: int | None = None, lessons: list[str] | None = None) -> dict:
    """The proven Wan 2.2 TI2V-5B text/image-to-video graph, lifted node-for-node from the
    spark video proof. UNETLoader → ModelSamplingSD3(shift 8) → KSampler(uni_pc / simple, 20
    steps, cfg 5.0, denoise 1.0); CLIPLoader(type ``wan``) + VAELoader feed a
    Wan22ImageToVideoLatent whose width/height clamp to [256,1280] and snap to the 32-px grid
    the sampler needs, and whose length clamps to [9,121] and snaps to 4n+1. A reference
    image, when given, wires LoadImage → start_image (image-to-video); with NONE the node
    AND its start_image key are omitted, for pure text-to-video (proof caveat). VAEDecode →
    CreateVideo(24 fps) → SaveVideo(mp4/h264). Pure dict builder, same shape as _fix_graph;
    cross-checked against zen-video/zen_wan_workflow.py build_prompt (not imported)."""
    try:
        w = max(256, min(1280, int(width)))
        h = max(256, min(1280, int(height)))
        n = max(9, min(121, int(length)))
    except (TypeError, ValueError):
        raise DispatchError("width, height and length must be numbers")  # → a clean 400
    w -= w % 32                                  # snap to the 32-px grid (proof caveat)
    h -= h % 32
    n = ((n - 1) // 4) * 4 + 1                    # snap to 4n+1
    pos = prompt.strip()
    neg = _WAN_NEG
    if lessons:
        neg = _WAN_NEG + ", " + ", ".join(lessons)  # negative-only (see _fix_graph)
    latent = {"vae": ["3", 0], "width": w, "height": h, "length": n, "batch_size": 1}
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": _WAN_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": _WAN_CLIP, "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _WAN_VAE}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": pos}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": neg}},
        "8": {"class_type": "Wan22ImageToVideoLatent", "inputs": latent},
        "9": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0],
              "negative": ["6", 0], "latent_image": ["8", 0],
              "seed": seed or random.randrange(2**31), "steps": 20, "cfg": 5.0,
              "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24}},
        "12": {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": prefix,
               "format": "mp4", "codec": "h264"}},
    }
    if ref_name:
        g["7"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        g["8"]["inputs"]["start_image"] = ["7", 0]   # TI2V; omitted entirely for pure t2v
    return g


def _image_graph(prompt: str, prefix: str, *, flow: dict | None = None,
                 width: int = 1024, height: int = 1024,
                 seed: int | None = None, lessons: list[str] | None = None) -> dict:
    """The proven Z-Image-Turbo text-to-image graph, lifted node-for-node from the shipped
    "Text to Image (Z-Image-Turbo)" blueprint. UNETLoader(z_image_turbo_bf16) →
    ModelSamplingAuraFlow(shift 3) → KSampler(res_multistep / simple, 4 steps, cfg 1.0,
    denoise 1.0) over EmptySD3LatentImage; CLIPLoader(qwen_3_4b, type ``lumina2``) +
    VAELoader(ae) feed the positive/negative CLIPTextEncode; VAEDecode → SaveImage.
    width/height clamp to [256,2048] and snap to the 16-px grid. Turbo means 4 steps at
    cfg 1.0 (a negative prompt is inert at cfg 1, but the node is kept for graph symmetry
    with the other builders and so lessons can steer via the positive). Pure dict builder,
    same shape as _video_graph — the text-to-image analogue the composer's default lane
    dispatches. Unlike _fix_graph this DOES start from an EmptySD3LatentImage: it is a true
    text-to-image create, not an edit of an existing asset."""
    try:
        w = max(256, min(2048, int(width)))
        h = max(256, min(2048, int(height)))
    except (TypeError, ValueError):
        raise DispatchError("width and height must be numbers")  # → a clean 400, not a 500
    w -= w % 16
    h -= h % 16
    pos = prompt.strip()
    # A flow lane frames the text-to-image prompt too (cfg 1.0 turbo means the
    # negative node is inert — steering happens in the positive, so the frame does).
    # Lessons do NOT ride the positive (see _fix_graph); with an inert negative the
    # turbo lane simply has no lesson channel — dispatch_image passes none anyway.
    if flow and flow.get("frame"):
        pos = f"{pos}. {flow['frame']}"
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": _ZIMAGE_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": _ZIMAGE_CLIP, "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _ZIMAGE_VAE}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": pos}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0],
              "negative": ["6", 0], "latent_image": ["7", 0],
              "seed": seed if seed is not None else random.randrange(2**31),
              "steps": 4, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def _model3d_graph(ref_name: str, prefix: str, *, seed: int | None = None) -> dict:
    """The proven Hunyuan3D-2.1 image-to-mesh graph, lifted node-for-node from ComfyUI's
    own 3d_hunyuan3d-v2.1 template. ImageOnlyCheckpointLoader loads MODEL+CLIP_VISION+VAE
    from the one bundled checkpoint (the separate-loader path produced noise); the image
    is CLIP-vision encoded → Hunyuan3Dv2Conditioning → ModelSamplingAuraFlow(shift 1) — the
    flow-matching wrapper WITHOUT which the sampler yields fragments — → KSampler(euler /
    normal, 30 steps, cfg 5, denoise 1) over EmptyLatentHunyuan3Dv2(4096) → VAEDecodeHunyuan3D
    → VoxelToMesh(surface net, 0.6) → SaveGLB. Verified on spark: a coherent 3D garment mesh.
    A reference image is REQUIRED — there is no text-to-3D path."""
    return {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": _HY3D_CKPT}},
        "2": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "3": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["1", 1], "image": ["2", 0], "crop": "center"}},
        "4": {"class_type": "Hunyuan3Dv2Conditioning", "inputs": {"clip_vision_output": ["3", 0]}},
        "5": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 1.0}},
        "6": {"class_type": "EmptyLatentHunyuan3Dv2", "inputs": {"resolution": 4096, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["4", 0], "negative": ["4", 1],
              "latent_image": ["6", 0], "seed": seed if seed is not None else random.randrange(2**31),
              "steps": 30, "cfg": 5.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "8": {"class_type": "VAEDecodeHunyuan3D", "inputs": {"samples": ["7", 0], "vae": ["1", 2], "num_chunks": 8000, "octree_resolution": 256}},
        "9": {"class_type": "VoxelToMesh", "inputs": {"voxel": ["8", 0], "algorithm": "surface net", "threshold": 0.6}},
        "10": {"class_type": "SaveGLB", "inputs": {"mesh": ["9", 0], "filename_prefix": prefix}},
    }


def _compose_graph(ref_names: list[str], instruction: str, prefix: str,
                   lessons: list[str] | None = None, flow: dict | None = None,
                   base: str | None = None, denoise: float | None = None) -> dict:
    """Multi-reference compose: several chosen assets become the references and the
    user's words say HOW to combine them into a new output. Same proven Qwen
    reference-conditioned wiring as _fix_graph (TextEncodeQwenImageEditPlus takes
    image1..imageN), so no new pipeline — the multi-input case of the good graph.

    ``base`` names the reference that is the SUBJECT rather than a source. Without it
    the sampler starts from an EMPTY latent at denoise 1.0 — it SYNTHESISES a new image
    guided by every reference equally, which is right for "combine these" and wrong for
    "put the garment from A onto the scene in B": that returned a ghosted double
    exposure of both photographs, because nothing told it which one it was making.
    With a base, that image is VAEEncode'd as the starting latent exactly as an edit is,
    so its pose, framing and composition SURVIVE and the others act only as references.
    ``denoise`` then means what it does everywhere else — how far the base may travel
    (a painting→photograph conversion needs the high band; a garment swap does not).
    """
    shape = (flow or {}).get("frame") or "Photorealistic, coherent single result."
    prompt = (f"{instruction.strip()}. Combine the provided reference images as "
              f"described. {shape}")
    g: dict = {
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": _QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": _QWEN_UNET, "weight_dtype": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _QWEN_VAE}},
    }
    pos_inputs: dict = {"clip": ["3", 0], "vae": ["5", 0], "prompt": prompt}
    scaled: dict[str, str] = {}   # reference name -> the node holding it, scaled
    nid = 20
    for i, name in enumerate(ref_names[:5], start=1):  # up to 5 references
        li, si = str(nid), str(nid + 1)
        g[li] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[si] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [li, 0]}}
        pos_inputs[f"image{i}"] = [si, 0]
        scaled.setdefault(name, si)
        nid += 2
    g["6"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": pos_inputs}
    neg_prompt = _NEG + (", " + flow["negative"] if flow and flow.get("negative") else "")
    if lessons:
        neg_prompt = neg_prompt + ", " + ", ".join(lessons)
    # The negative sees the SAME references as the positive. TextEncodeQwenImageEditPlus
    # does not just encode words: it attaches a reference latent per image. A negative
    # encoded without them is a different STRUCTURE, not merely different words, so every
    # CFG step subtracted a term computed over a latent the positive did not share —
    # which is what turned "put this garment on that scene" into the two photographs
    # ghosted over each other under a stipple. _fix_graph has always mirrored them
    # (node 7 carries vae + image1); the multi-input case simply never did.
    neg_inputs = dict(pos_inputs)
    neg_inputs["prompt"] = neg_prompt
    g["7"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": neg_inputs}
    # Qwen's native multi-reference conditioning (image1..imageN on node 6) — NO
    # FluxKontextMultiReferenceLatentMethod: the same Flux-Kontext override that
    # stippled the single-image fix has no place on Qwen conditioning here either.
    if base in scaled:
        # The base IS the subject: encode the reference already loaded for it as the
        # starting latent (the same wiring an edit uses) so its pose and framing survive
        # instead of being averaged away. It stays a reference too — one load, one slot.
        g["10"] = {"class_type": "VAEEncode", "inputs": {"pixels": [scaled[base], 0], "vae": ["5", 0]}}
    else:
        g["10"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1536, "batch_size": 1}}
    g["11"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.1}}
    g["12"] = {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}}
    g["13"] = {"class_type": "KSampler", "inputs": {"model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0],
               "seed": random.randrange(2**31), "steps": 30, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple",
               "denoise": (_edit_denoise(denoise, default=(flow or {}).get("denoise", CONVERSION_DENOISE))
                           if base in scaled else 1.0)}}
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
    # Forward the caller's identity AND their per-GPU target (X-Target-GPU) so a fix/
    # compose/video/rerun dispatched server-side lands on the machine the user picked —
    # one targeting path, honored by dispatch_if_worker for every generation surface.
    for h in ("Authorization", "Cookie", "X-Target-GPU"):
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
        elif ct == "Wan22ImageToVideoLatent":
            p.update(width=ins.get("width"), height=ins.get("height"), length=ins.get("length"))
        elif ct == "CreateVideo":
            p.update(fps=ins.get("fps"))
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


# ── Authoritative gpu-jobs queue (the tasks engine is the source of truth) ───────────
# The visible queue reads the org's activities STRAIGHT from the cloud tasks engine
# (GET /v1/tasks/namespaces/gpu-jobs/activities, org-scoped by the caller's bearer via
# the tasks gate) — not a local shadow. So every row shows the REAL worker that claimed
# it (activity.identity) and its REAL status, and nothing renders on a GPU without
# appearing here (a claimed job IS an activity). render_jobs.json survives ONLY as an
# optimistic placeholder for the ~2s between enqueue and first claim.
_JOBS_NS = os.environ.get("STUDIO_GPU_JOBS_NS", "gpu-jobs")
_PHASE = {"SCHEDULED": "queued", "PENDING": "queued", "STARTED": "running",
          "RUNNING": "running", "COMPLETED": "done", "SUCCEEDED": "done",
          "FAILED": "failed", "CANCELED": "cancelled", "CANCELLED": "cancelled",
          # An engine-expired job is a FAILURE the user must see — mapping it to
          # "queued" (the old default) made expired work silently vanish.
          "TIMED_OUT": "failed", "TIMED_OUT_SCHEDULE_TO_START": "failed",
          "EXPIRED": "failed", "TIMEDOUT": "failed"}


def _epoch(ts) -> int | None:
    """Best-effort seconds-since-epoch from a tasks timestamp (RFC3339 string or a
    number in s/ms). None for empty/unset so age math can skip it."""
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return int(ts if ts < 1e12 else ts / 1000)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _act_type_name(a: dict) -> str:
    t = a.get("type") or a.get("activityType") or {}
    return t.get("name", "") if isinstance(t, dict) else ""


def _activity_phase(a: dict) -> str:
    # The engine emits long-form enum names (ACTIVITY_TASK_STATE_CANCELED); the map
    # is keyed bare. Without the strip EVERY status missed the map and defaulted to
    # "queued" — 400+ terminal jobs rendered as a phantom queue.
    s = str(a.get("status", "")).upper().removeprefix("ACTIVITY_TASK_STATE_")
    return _PHASE.get(s, "queued")


def _prompt_title_refs(prompt: dict) -> tuple:
    """A human title (SaveImage/SaveVideo prefix) + first LoadImage ref for a thumbnail,
    derived from the activity's OWN input so a row is self-describing without the shadow."""
    from middleware.gpu_dispatch import SAVE_NODES
    title, refs = "render", []
    for n in (prompt or {}).values():
        if not isinstance(n, dict):
            continue
        ct = n.get("class_type", "")
        if ct in SAVE_NODES:
            title = (n.get("inputs", {}) or {}).get("filename_prefix") or title
        elif "LoadImage" in ct:
            im = (n.get("inputs", {}) or {}).get("image")
            if im:
                refs.append(im)
    return title.split("/")[-1], refs[:1]


def _job_from_activity(a: dict, now: int) -> dict:
    """Map a StandaloneActivity → the UI's job row: REAL claimer + REAL status."""
    ex = a.get("execution") or {}
    pid = ex.get("runId") or ex.get("workflowId") or a.get("activityId") or ""
    inp = a.get("input") if isinstance(a.get("input"), dict) else {}
    title, refs = _prompt_title_refs(inp.get("prompt") or {})
    tq = str(a.get("taskQueue") or "")
    # identity is the machine that CLAIMED it; before pick-up the "gpu:<name>" lane
    # still names the targeted machine so a queued targeted job shows where it's headed.
    node = a.get("identity") or (tq[4:] if tq.startswith("gpu:") else None) or "queued"
    start = _epoch(a.get("startTime")) or _epoch(a.get("scheduleTime"))
    beat = _epoch(a.get("lastHeartbeatTime"))
    return {"id": pid, "node": node, "status": _activity_phase(a),
            "prefix": title, "refs": refs,
            "age": max(0, now - start) if start else 0,
            "beat_age": (now - beat) if beat else None}


def _fold_engine(org: str, rows: list[dict], acts: list | None) -> None:
    """Fold what the ENGINE knows into worklog rows — the states no local observation
    can produce:

    * failed / cancelled: such a job leaves no landed output, so _finalize_landed can
      never close it and without this fold it reads queued forever (the lying state).
    * running: a worker CLAIMED and STARTED it. That is a fact, not optimism, and
      without it a render actively sampling on a GPU still reads "queued" — the same
      class of lie from the other direction.

    ``done`` is NOT folded: only a landed, intact output makes done (the integrity gate
    owns that verdict). Mutates rows in place (mirrors the persisted status)."""
    if not acts:
        return
    verdict = {}
    for a in acts:
        ex = a.get("execution") or {}
        pid = ex.get("runId") or ex.get("workflowId") or a.get("activityId") or ""
        if pid:
            verdict[pid] = (_activity_phase(a), str(a.get("failureCause") or "")[:300])
    for r in rows:
        if r.get("status") in ("done", "failed", "cancelled"):
            continue
        ph, why = verdict.get(r.get("id"), (None, ""))
        if ph == "failed":
            worklog.set_status(org, r["id"], "failed",
                               note=why or "render failed on the GPU worker")
            r["status"] = "failed"
        elif ph == "cancelled":
            worklog.set_status(org, r["id"], "cancelled")
            r["status"] = "cancelled"
        elif ph == "running" and r.get("status") == "queued":
            worklog.set_status(org, r["id"], "running")
            worklog.mark_started(org, r["id"], int(time.time()))   # idempotent; feeds the ETA median
            r["status"] = "running"


async def _list_gpu_jobs(request) -> list | None:
    """The caller-org's gpu-jobs activities from the tasks engine. Returns None (not [])
    when the tasks API can't be reached, so a transient blip falls back to the local
    placeholder rather than flashing an empty queue. Tenant-scoped by the bearer."""
    from middleware.gpu_dispatch import CLOUD, _bearer
    tok = _bearer(request)
    if not tok:
        return []
    url = f"{CLOUD}/v1/tasks/namespaces/{_JOBS_NS}/activities?pageSize=100"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url, headers={"Authorization": tok}) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None
    acts = data.get("activities") if isinstance(data, dict) else None
    return acts if isinstance(acts, list) else []


def _render_queue_placeholder(root, now: int, exclude: set) -> list:
    """Optimistic pre-claim rows from render_jobs.json: ONLY jobs the authoritative list
    hasn't surfaced yet and that are seconds-young — the bridge across the ~2s between
    enqueue and first claim. Anything older is the engine's to report."""
    try:
        jobs = json.loads((root / "render_jobs.json").read_text())
    except Exception:
        return []
    out = []
    for j in jobs:
        pid = j.get("id")
        if not pid or pid in exclude:
            continue
        age = now - int(j.get("ts", 0))
        if age < 0 or age > 30:
            continue
        out.append({"id": pid, "node": j.get("node") or "queued", "status": "queued",
                    "prefix": j.get("prefix") or "render", "refs": (j.get("refs") or [])[:1],
                    "age": age, "beat_age": None})
    return out


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
    if pid.startswith("held-"):
        # A held job cancels by deleting its spool file — else the next queue
        # read would faithfully re-enqueue the very job the user just cancelled.
        (_held_dir(org) / f"{pid}.json").unlink(missing_ok=True)
        realm = "held"
    worklog.set_status(org, pid, "cancelled")
    return realm


async def _cancel_activity(request, pid: str) -> bool:
    """Cancel the BYO-GPU activity in the cloud tasks engine — the counterpart of
    gpu_dispatch's enqueue, POST …/activities/{id}/{id}/cancel (activityId == runId
    == prompt_id by construction). Without this, a "cancelled" SCHEDULED job was
    only hidden locally and still ran (and billed) the moment a worker connected.
    Best-effort: a cancel of an already-claimed/finished activity is a no-op."""
    from middleware.gpu_dispatch import CLOUD, _bearer
    if request is None or not pid:
        return False
    tok = _bearer(request)
    if not tok:
        return False
    url = f"{CLOUD}/v1/tasks/namespaces/{_JOBS_NS}/activities/{pid}/{pid}/cancel"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(url, headers={"Authorization": tok},
                              json={"reason": "cancelled in Studio", "identity": "studio"}) as r:
                return 200 <= r.status < 300
    except Exception as e:
        log.warning("gpu-job cancel did not reach the tasks engine for %s: %s", pid, e)
        return False


def _landed_output(root: Path | None, prefix: str) -> str | None:
    """Library-relative path of the newest file a SaveImage prefix produced, or None
    if it hasn't landed yet. Lets the work-log report done + wire lineage edges."""
    if root is None or not prefix:
        return None
    try:
        parent = root / os.path.dirname(prefix)
        stem = os.path.basename(prefix)
        # Match a saver's exact "<prefix>_<counter>_.<ext>" (png / mp4 / webm) — NOT a
        # loose "<prefix>*" which also matches a CHILD prefix ("a" would swallow
        # "a_00001__00001_"), collapsing distinct lineage steps onto one output.
        pat = re.compile(r"^" + re.escape(stem) + r"_\d+_\.(?:png|mp4|webm)$")
        hits = [p for p in parent.glob(stem + "_*_.*") if pat.match(p.name)]
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


# ── Dispatch idempotency: collapse rapid identical re-submits (a double-fired button,
# an unguarded caller, a retried request) onto ONE render instead of billing a second
# GPU job — the single biggest source of "the same image generated over and over".
# Keyed by (org, output prefix, instruction, refs); in-memory is enough because the
# duplicates arrive within seconds on the same pod. ─────────────────────────────────
_RECENT_DISPATCH: dict = {}          # key -> (ts, prompt_id)
_DEDUP_WINDOW_S = 60


def _dedup_key(org: str, kind: str, prompt: str, refs: list, uploads: list) -> str:
    # Identify a render by its STABLE inputs — NOT output_prefix, which every lane
    # stamps with a random _job_tag() before dispatch (so keying on it made two
    # identical submits always miss → the anti-double-bill guard never fired). This
    # keys on what actually defines the render: org, lane, prompt, and its inputs.
    import hashlib
    raw = "|".join([org, kind or "", (prompt or "").strip(),
                    ",".join(sorted(refs or [])), ",".join(sorted(uploads or []))])
    return hashlib.md5(raw.encode()).hexdigest()


def _recent_dispatch(key: str) -> str | None:
    now = time.time()
    for k, (ts, _pid) in list(_RECENT_DISPATCH.items()):   # prune so the map stays bounded
        if now - ts > _DEDUP_WINDOW_S:
            _RECENT_DISPATCH.pop(k, None)
    hit = _RECENT_DISPATCH.get(key)
    return hit[1] if hit and hit[1] and now - hit[0] < _DEDUP_WINDOW_S else None


# TRANSPORT failures only — the fabric was unreachable, so the SAME job will succeed
# unchanged once it is reachable. That is the whole premise of holding: retry the
# identical graph later.
#
# "was not queued" was in this list and is NOT a transport failure. The engine returns
# it for a graph it REFUSED (a missing model, a stale staged input) — a refusal that
# will repeat forever. Because _retry_held stops the sweep on a holdable error, one such
# job pinned the entire spool: observed in prod as ten jobs that never moved across
# hours of healthy fabric, while fresh dispatches went through beside them. A job the
# engine will never accept must FAIL with its reason, not wait for a condition that has
# already been met.
_HOLDABLE = ("momentarily unavailable", "no gpu worker online",
             "could not reach the render engine", "no available server",
             "connection refused", "timed out")

# How many held jobs one sweep may FAIL to enqueue before it gives up until the next
# read. The enqueue is a synchronous urlopen on the event loop, so an unbounded sweep
# is an unbounded stall: ten entries each shipping a ~25MB body that the far end reset
# blocked every request in the process, the readiness probe included, and the pod went
# unready behind a 503. Small enough that a read stays quick, more than one so a stuck
# entry cannot pin the queue.
_HOLD_TRIES = 2


def _held_dir(org: str) -> Path:
    """orgs/<org>/held — the spool of jobs the render fabric could not be reached
    for. Same per-org tree as templates/worklog, so tenancy is structural."""
    out = Path(folder_paths.get_org_output_directory(org))
    base = out.parent if out.parent.name == org else out
    d = base / "held"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hold_job(org: str, graph: dict, common: dict) -> str:
    """Persist a job the fabric was unreachable for, as a QUEUED row + a spool file
    carrying the full graph — the user's prompt/edit is never lost to a transient
    'GPU worker unavailable'. Returns the held prompt_id (held-…)."""
    pid = "held-" + uuid.uuid4().hex[:12]
    rec = {"id": pid, "ts": int(time.time()), "graph": graph, **common}
    p = _held_dir(org) / f"{pid}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    tmp.replace(p)
    worklog.record(org, pid=pid, status="queued", lane="gpu", node="waiting", **common)
    _trace("dispatch.held", org=org, pid=pid, kind=common.get("kind"),
           prefix=common.get("output_prefix"))
    return pid


def _list_held(org: str) -> list[dict]:
    out = []
    try:
        for p in sorted(_held_dir(org).glob("held-*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
    except Exception:
        pass
    return out


async def _retry_held(request, server, org: str) -> None:
    """Re-enqueue spooled jobs with the CURRENT caller's credentials (same org —
    the routes resolve org server-side). Runs on every queue read, so the act of
    watching the queue heals it; no credential is ever stored at rest. Success
    replaces the held row with a real queued row under the fabric's prompt_id."""
    tries = 0
    for rec in _list_held(org):
        graph, held_pid = rec.get("graph") or {}, rec.get("id", "")
        common = {k: rec.get(k) for k in
                  ("kind", "prompt", "refs", "uploads", "parents", "output_prefix", "params")}
        try:
            pid = await _queue_prompt(request, server, graph)
        except DispatchError as e:
            # Distinguish "the fabric is down" from "THIS job cannot be re-queued".
            # Returning on any error let ONE un-requeueable entry strand every other
            # held job forever (observed: the same 5 ids across every queue read, none
            # ever moving). A transport failure still stops the sweep — the rest would
            # fail identically and re-holding them churns. Anything else is this job's
            # own problem: fail it with the reason (never silently) and keep going, so
            # a single poisoned entry can't hold the queue hostage.
            #
            # A holdable MESSAGE is not proof of a holdable CONDITION. "momentarily
            # unavailable" is what the dispatcher says for ANY enqueue failure — it
            # names the fleet as the cause without checking — so a job that simply
            # cannot be enqueued (a 4K source whose inlined body exceeds the API's
            # request limit) wore transport words and re-held forever. The spool is
            # walked in sorted order, so the alphabetically-first of those pinned all
            # ten across two days while both GPUs sat idle and healthy.
            #
            # So the sweep TRIES THE REST before believing it — but only _HOLD_TRIES of
            # them per read. Trying all of them took studio DOWN: the enqueue is a
            # SYNCHRONOUS urlopen on the event loop, and ten of those, each shipping a
            # ~25MB body that the far end resets, blocks every other request in the
            # process — including the readiness probe, so the pod went unready and the
            # ingress served 503. The bound is what makes this safe to do at all: each
            # read makes progress past a stuck entry without any single read being able
            # to stall the server.
            if any(s in str(e).lower() for s in _HOLDABLE):
                _trace("dispatch.held_again", org=org, held=held_pid, err=str(e)[:120])
                tries += 1
                if tries >= _HOLD_TRIES:
                    return
                continue
            (_held_dir(org) / f"{held_pid}.json").unlink(missing_ok=True)
            worklog.set_status(org, held_pid, "failed", note=f"could not re-queue: {e}")
            _trace("dispatch.held_unqueueable", org=org, held=held_pid, err=str(e)[:120])
            continue
        (_held_dir(org) / f"{held_pid}.json").unlink(missing_ok=True)
        worklog.remove(org, held_pid)
        lane = "gpu" if _in_render_jobs(org, pid) else "local"
        node = _render_job_node(org, pid) if lane == "gpu" else "studio pod"
        worklog.record(org, pid=pid, status="queued", lane=lane, node=node, **common)
        _trace("dispatch.held_requeued", org=org, held=held_pid, pid=pid)


async def _dispatch(request, server, org: str, graph: dict, *, kind: str, prompt: str,
                    refs: list, uploads: list, parents: list, output_prefix: str,
                    lessons: list | None = None) -> str:
    """The ONE place a render is queued AND recorded. On success writes a `queued`
    work-log row (lane detected). A TRANSPORT failure (fabric/tasks engine
    unreachable — the messages in _HOLDABLE) HOLDS the job instead of failing it:
    the prompt/edit lands in the visible queue and re-enqueues when the fabric is
    back. A validation failure still writes a `failed` row and raises — so a
    dispatch is NEVER invisible, whatever lane it took or however it failed.
    Idempotent: an identical dispatch within the dedup window returns the in-flight
    prompt_id instead of billing a second render."""
    params = _graph_params(graph)
    if lessons:
        params["lessons"] = lessons   # what the user's notes taught THIS job to avoid
    common = dict(kind=kind, prompt=prompt, refs=refs, uploads=uploads,
                  parents=parents, output_prefix=output_prefix, params=params)
    key = _dedup_key(org, kind, prompt, refs, uploads)
    dup = _recent_dispatch(key)
    if dup:
        return dup                    # identical render already in flight — reuse it
    try:
        pid = await _queue_prompt(request, server, graph)
    except DispatchError as e:
        msg = str(e).lower()
        if any(s in msg for s in _HOLDABLE):
            pid = _hold_job(org, graph, common)
            _RECENT_DISPATCH[key] = (time.time(), pid)
            return pid
        worklog.record(org, status="failed", error=str(e), **common)
        raise
    _RECENT_DISPATCH[key] = (time.time(), pid)
    lane = "gpu" if _in_render_jobs(org, pid) else "local"
    node = _render_job_node(org, pid) if lane == "gpu" else "studio pod"
    worklog.record(org, pid=pid, status="queued", lane=lane, node=node, **common)
    _trace("dispatch.queued", org=org, pid=pid, kind=kind, lane=lane, node=node,
           refs=len(refs), parents=",".join(parents) or None, prefix=output_prefix)
    return pid


async def dispatch_fix(request: web.Request, server, org: str, rel: str,
                       instruction: str, upload: str | None = None,
                       stack: str | None = None, strength=None, seed=None,
                       mask: str | None = None, flow_kind: str | None = None) -> dict:
    """The EDIT action — a LOW-denoise Qwen edit of an already-generated gallery asset.
    The base is EITHER a library asset (``rel``, its stable path — the generated output
    itself) OR a just-uploaded image (``upload``). The selected generated image is the
    SOURCE: it is VAEEncode'd and used as the KSampler latent (never EmptyLatentImage),
    at ``strength``→denoise (default 0.25, edit band [0.05,0.95] — a true 1.0 regenerate
    is a SEPARATE action). ``seed`` reuses the source's ORIGINAL seed by default (stable
    iteration); a caller may vary it and still edit the source. ``mask`` (an uploaded
    painted image, white=edit-here) switches to the masked-inpaint graph that keeps
    pixels OUTSIDE the mask byte-identical. The output is a CHILD version of the source
    (parents=[rel]). The ONE edit path: /v1/library/fix + the chat `create` capability."""
    instruction = (instruction or "").strip()
    if not 3 <= len(instruction) <= 500:
        raise DispatchError("instruction required (3-500 chars)")
    flow = _flow_of(flow_kind)
    # A flow lane sets the denoise its conversion needs (a drawing must be nearly
    # fully re-rendered to become a photograph); an explicit strength still wins.
    # Distance, in priority order: an explicit strength wins; else the lane's own
    # conversion denoise; else the INSTRUCTION's own request ("make this into a CAD
    # drawing" must actually convert — at the edit default it returned the source
    # untouched); else the preserving edit default.
    lane_default = (flow or {}).get("denoise")
    if lane_default is None:
        lane_default = CONVERSION_DENOISE if asks_conversion(instruction) else EDIT_DENOISE_DEFAULT
    denoise = _edit_denoise(strength, default=lane_default)
    src_seed = None                          # the source's original seed, reused by default
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
        # FULL-RES source (the real output file, never the .thumbs/ preview), staged under
        # a content-addressed name. Reuse the source's ORIGINAL seed from its embedded graph.
        ref_name = _stage_source(in_dir, p, "fix")
        stem = re.sub(r"[^a-zA-Z0-9_-]", "_", p.stem)[:48]
        try:
            sp = _provenance(p).get("seed")
            src_seed = int(sp) if sp is not None else None
        except (TypeError, ValueError):
            src_seed = None
    # Seed: an explicit caller seed wins; else reuse the source's original (stable
    # iteration); else the graph mints one. A varied seed STILL edits the source (the
    # VAEEncode latent is the KSampler input regardless of seed).
    try:
        eff_seed = int(seed) if seed is not None else src_seed
    except (TypeError, ValueError):
        eff_seed = src_seed
    # Masked edit (Edit selected area): stage the painted mask like the source.
    mask_name = None
    if mask:
        mnames = _resolve_uploads(org, [mask])
        if not mnames:
            raise DispatchError(f"unknown mask: {mask}")
        mask_name = mnames[0]
    stem = f"{stem}_{_job_tag()}"
    lessons = _lessons(org, _library_root(org), [] if upload else [rel])
    if mask_name:
        graph = _inpaint_graph(ref_name, mask_name, instruction, f"fixes/{stem}",
                               flow=flow, denoise=denoise, seed=eff_seed, lessons=lessons)
        kind = "edit_area"
    else:
        graph = _fix_graph(ref_name, instruction, f"fixes/{stem}",
                           flow=flow, denoise=denoise, seed=eff_seed, lessons=lessons,
                           size=_source_size(in_dir / ref_name))
        # The work-log/queue row carries the LANE the user picked (cad_ghost,
        # product, …) so the queue shows the real flow, not a generic "fix".
        kind = flow_kind if flow else "fix"
    # INVARIANT: an Edit dispatch can NEVER be a text-to-image regenerate.
    bad = _assert_edit_graph(graph)
    if bad:
        raise DispatchError(f"edit rejected: {bad}")
    _trace("edit.build", org=org, op=kind, source=(None if upload else rel),
           ref=ref_name, denoise=denoise, seed=eff_seed,
           prefix=f"fixes/{stem}", lessons=len(lessons))
    pid = await _dispatch(request, server, org, graph,
                          lessons=lessons, kind=kind, prompt=instruction,
                          refs=[ref_name] + ([mask_name] if mask_name else []),
                          uploads=[x for x in (upload, mask) if x],
                          parents=[] if upload else [rel],
                          output_prefix=f"fixes/{stem}")
    if stack:
        stacks_store.route_prefix(org, f"fixes/{stem}", stack, added_by=_owner_of(request))
    out = {"ok": True, "prompt_id": pid, "output_prefix": f"fixes/{stem}",
           "operation": kind, "denoise": denoise, "seed": eff_seed, "parent": None if upload else rel}
    if note:
        out["note"] = note
    return out


async def dispatch_video(request: web.Request, server, org: str, prompt: str,
                         rel: str | None = None, upload: str | None = None,
                         width: int | None = None, height: int | None = None,
                         length: int | None = None, seed: int | None = None,
                         stack: str | None = None) -> dict:
    """Generate a video from a text prompt and AT MOST ONE reference image — the proven
    Wan 2.2 TI2V-5B graph. The reference is EITHER a library asset (``rel``, staged into the
    org input dir like fix) OR an image just uploaded via ``/upload/image`` (``upload``);
    with neither it is pure text-to-video. The ONE video path: the /v1/library/video route
    (and, later, the chat `create` capability) both call this.

    No lane-forcing here — _dispatch's self-POST already routes to gpu-jobs when a BYO GPU is
    online and surfaces the real DispatchError ("unet_name … not in []" / "no GPU worker")
    when not, so the model-locality reality (wan2.2 lives on spark) rides the existing
    advisory rather than a second gate."""
    prompt = (prompt or "").strip()
    if not 3 <= len(prompt) <= 500:
        raise DispatchError("prompt required (3-500 chars)")
    if upload and rel:
        raise DispatchError("use at most one reference (a library image OR an upload)")
    root = _library_root(org)
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    ref_name = None
    staged = False
    if upload:
        names = _resolve_uploads(org, [upload])
        if not names:
            raise DispatchError(f"unknown upload: {upload}")
        ref_name = names[0]
    elif rel:
        # Iteration hygiene, same as fix: a corrupt base falls back to its clean ancestor.
        rel, _sub = _clean_base(org, root, rel)
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        ref_name = _stage_source(in_dir, p, "video")
        staged = True
    base = Path(ref_name).stem if ref_name else " ".join(prompt.split()[:8])
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", base)[:48] + f"_{_job_tag()}"
    lessons = _lessons(org, root, [rel] if rel else [])
    pid = await _dispatch(
        request, server, org,
        _video_graph(prompt, f"video/{stem}", ref_name=ref_name,
                     width=width or 640, height=height or 640, length=length or 73,
                     seed=seed, lessons=lessons),
        lessons=lessons, kind="video", prompt=prompt,
        refs=[ref_name] if staged else [], uploads=[upload] if upload else [],
        parents=[rel] if rel else [], output_prefix=f"video/{stem}")
    if stack:
        stacks_store.route_prefix(org, f"video/{stem}", stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "output_prefix": f"video/{stem}"}


async def dispatch_image(request: web.Request, server, org: str, prompt: str,
                         width: int | None = None, height: int | None = None,
                         seed: int | None = None, stack: str | None = None,
                         flow_kind: str | None = None) -> dict:
    """Generate an IMAGE from a text prompt — the proven Z-Image-Turbo text-to-image graph.
    This is the composer's default "What should we create?" lane: no reference image (that
    is fix/compose), pure text-to-image through the ONE dispatch funnel. Org is the
    bearer-token claim (IAM middleware) ONLY — an ``org`` field in the body/query is never
    read. Like video/fix, a model-less pod surfaces the real DispatchError ("unet_name …
    not in []" / "no GPU worker") rather than a silent nothing, so the composer can ALWAYS
    show a queued/generating state or an honest error."""
    prompt = (prompt or "").strip()
    if not 3 <= len(prompt) <= 2000:
        raise DispatchError("describe what to create (3-2000 chars)")
    root = _library_root(org)
    flow = _flow_of(flow_kind)
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", " ".join(prompt.split()[:8]))[:48] + f"_{_job_tag()}"
    prefix = f"image/{stem}"
    lessons = _lessons(org, root, [])
    pid = await _dispatch(
        request, server, org,
        _image_graph(prompt, prefix, flow=flow, width=width or 1024, height=height or 1024,
                     seed=seed, lessons=lessons),
        lessons=lessons, kind=(flow_kind if flow else "image"), prompt=prompt,
        refs=[], uploads=[], parents=[], output_prefix=prefix)
    if stack:
        stacks_store.route_prefix(org, prefix, stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "output_prefix": prefix}


async def dispatch_model3d(request: web.Request, server, org: str,
                           rel: str | None = None, upload: str | None = None,
                           seed: int | None = None, stack: str | None = None) -> dict:
    """Generate a 3D mesh (.glb) from ONE reference image — the proven Hunyuan3D-2.1 graph.
    The reference is EITHER a library asset (``rel``) OR an upload (``upload``); one is
    REQUIRED (no text-to-3D). Same dispatch/gpu-jobs/flywheel path as fix/video; the model
    (hunyuan_3d_v2.1) lives on the worker, so model-locality rides the existing advisory."""
    if not rel and not upload:
        raise DispatchError("a reference image is required for 3D")
    if upload and rel:
        raise DispatchError("use at most one reference (a library image OR an upload)")
    root = _library_root(org)
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    staged = False
    if upload:
        names = _resolve_uploads(org, [upload])
        if not names:
            raise DispatchError(f"unknown upload: {upload}")
        ref_name = names[0]
    else:
        rel, _sub = _clean_base(org, root, rel)
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        # Hunyuan3D encodes the image at ~518px internally, so a 4k catalog master is
        # pure waste — and inlining a ~20MB PNG to the worker overruns the gpu-jobs
        # enqueue (the "GPU worker momentarily unavailable" fall-through). Cap the
        # staged copy at 1536px; the mesh is identical, the dispatch is reliable.
        ref_name = _stage_source(in_dir, p, "model3d", max_px=1536)
        staged = True
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(ref_name).stem)[:48] + f"_{_job_tag()}"
    pid = await _dispatch(
        request, server, org, _model3d_graph(ref_name, f"model3d/{stem}", seed=seed),
        kind="model3d", prompt="3D mesh from " + (rel or upload or "image"),
        refs=[ref_name] if staged else [], uploads=[upload] if upload else [],
        parents=[rel] if rel else [], output_prefix=f"model3d/{stem}")
    if stack:
        stacks_store.route_prefix(org, f"model3d/{stem}", stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "output_prefix": f"model3d/{stem}"}


async def dispatch_compose(request: web.Request, server, org: str, paths: list,
                           instruction: str, uploads: list | None = None,
                           stack: str | None = None, flow_kind: str | None = None,
                           base: str | None = None, strength=None) -> dict:
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
    # `base` names which of the chosen assets is the SUBJECT (the one being changed);
    # the rest stay references. Matched against the caller's own paths so it can only
    # ever be one of them, and moved to the front — Qwen reads image1 as the primary,
    # so the subject leads the conditioning as well as seeding the latent.
    if base:
        base = str(base).strip().lstrip("/")
        if base not in paths:
            raise DispatchError(f"base must be one of the chosen references: {base}")
        paths = [base] + [p for p in paths if p != base]
    root = _library_root(org)
    in_dir = Path(folder_paths.get_org_input_directory(org))
    in_dir.mkdir(parents=True, exist_ok=True)
    ref_names = []
    for rel in paths:
        p = _safe_asset(root, rel) if root else None
        if p is None or p.suffix.lower() != ".png":
            raise DispatchError(f"unknown asset: {rel}")
        nm = _stage_source(in_dir, p, "compose")
        ref_names.append(nm)
    ref_names.extend(uploaded)
    labels = [Path(x).stem for x in paths] + [Path(u).stem for u in uploaded]
    stem = "compose_" + "_".join(re.sub(r"[^a-zA-Z0-9]", "", s)[:12] for s in labels[:2]) + f"_{_job_tag()}"
    lessons = _lessons(org, root, list(paths))
    flow = _flow_of(flow_kind)
    base_name = ref_names[0] if base else None
    pid = await _dispatch(request, server, org,
                          _compose_graph(ref_names, instruction, f"composes/{stem}", lessons=lessons, flow=flow,
                                         base=base_name, denoise=strength),
                          lessons=lessons, kind=(flow_kind if flow else "compose"), prompt=instruction, refs=ref_names,
                          uploads=uploaded, parents=list(paths), output_prefix=f"composes/{stem}")
    if stack:
        stacks_store.route_prefix(org, f"composes/{stem}", stack, added_by=_owner_of(request))
    return {"ok": True, "prompt_id": pid, "output_prefix": f"composes/{stem}", "refs": len(ref_names)}


# Which dispatch made a row, read from the output prefix it recorded. The `kind` field
# carries the FLOW when one was active (product / cad_ghost / …), so it cannot name the
# operation — the prefix can, and every dispatch writes one.
_OP_BY_PREFIX = (("fixes/", "fix"), ("composes/", "compose"), ("video/", "video"),
                 ("model3d/", "model3d"), ("image/", "image"))


def _op_of(row: dict) -> str:
    prefix = str(row.get("output_prefix") or "")
    for pre, op in _OP_BY_PREFIX:
        if prefix.startswith(pre):
            return op
    return ""


async def retry_row(request: web.Request, server, org: str, row: dict) -> dict:
    """Re-dispatch ONE recorded render through the SAME funnel that would run it today.

    This is the whole point: a row that failed under old behavior (or during an outage)
    replays through ``dispatch_*``, so it picks up every CURRENT rule — the per-flow frames,
    the construction-lines ban in the global negative, the lane denoise, the output-integrity
    gate — with no second code path to keep in sync. The row's own record supplies the
    inputs (operation from the output prefix, flow from ``kind``, prompt, sources); nothing
    is inferred. When the record cannot reconstruct the job faithfully we REFUSE with the
    reason rather than render an approximation the user didn't ask for."""
    op = _op_of(row)
    prompt = (row.get("prompt") or "").strip()
    parents = [p for p in (row.get("parents") or []) if p]
    uploads = [u for u in (row.get("uploads") or []) if u]
    flow_kind = row.get("kind") if _flow_of(row.get("kind")) else None
    if op == "fix":
        if not parents:
            raise DispatchError("cannot retry: the original source asset was not recorded")
        return await dispatch_fix(request, server, org, parents[0], prompt, flow_kind=flow_kind)
    if op == "compose":
        if len(parents) + len(uploads) < 2:
            raise DispatchError("cannot retry: a compose needs 2-5 recorded sources, "
                                f"this row has {len(parents) + len(uploads)}")
        return await dispatch_compose(request, server, org, parents, prompt,
                                      uploads=uploads, flow_kind=flow_kind)
    if op == "video":
        return await dispatch_video(request, server, org, prompt,
                                    rel=parents[0] if parents else None)
    if op == "model3d":
        if not parents and not uploads:
            raise DispatchError("cannot retry: 3D needs its recorded reference image")
        return await dispatch_model3d(request, server, org,
                                      rel=parents[0] if parents else None,
                                      upload=None if parents else uploads[0])
    if op == "image":
        return await dispatch_image(request, server, org, prompt, flow_kind=flow_kind)
    raise DispatchError(f"cannot retry: unrecognized job type for prefix {row.get('output_prefix')!r}")


class NotOwned(DispatchError):
    """The prompt_id isn't the caller's — routes map this to 404 (never reveals it)."""


async def dispatch_rerun(request: web.Request, server, org: str, rel: str, *,
                         prompt: str | None = None, paths: list | None = None,
                         uploads: list | None = None, count=1,
                         stack: str | None = None) -> dict:
    """Re-render an asset's OWN embedded workflow, with optional PRECISE overrides:
    ``prompt`` replaces the text on the encoders the samplers condition POSITIVELY
    on (identified by sampler wiring — text comparison misreads a lessons-augmented
    negative, and the negative must never be overwritten); ``paths``/``uploads``
    re-point the graph's LoadImage nodes in order. Everything else in the workflow
    stays exactly as it was — this is the ONE precise-flow implementation, shared
    by POST /v1/library/rerun and the chat `rerun` tool."""
    root = _library_root(org)
    p = _safe_asset(root, rel) if root else None
    if p is None or p.suffix.lower() not in _SAVE_EXT:
        raise NotOwned(f"unknown asset: {rel}")
    try:
        graph = json.loads(_embedded(p).get("prompt", "null"))   # PNG OR video metadata
    except Exception:
        graph = None
    if not isinstance(graph, dict) or not graph:
        raise DispatchError("no embedded workflow in this output")
    pos_ids = _positive_encoders(graph)
    out_prefix = ""
    instr = ""
    for nid, n in graph.items():
        if not isinstance(n, dict):
            continue
        if n.get("class_type") in _SAVERS:
            out_prefix = n.get("inputs", {}).get("filename_prefix", "") or out_prefix
        elif nid in pos_ids and not instr:
            instr = n.get("inputs", {}).get("prompt") or n.get("inputs", {}).get("text") or ""
    new_prompt = (prompt or "").strip()
    if new_prompt:
        if not 3 <= len(new_prompt) <= 2000:
            raise DispatchError("prompt must be 3-2000 chars")
        for nid in pos_ids:
            ins = graph.get(nid, {}).setdefault("inputs", {})
            ins["prompt" if "prompt" in ins else "text"] = new_prompt
        instr = new_prompt
    ref_names = []
    for prel in (paths or []):
        rp = _safe_asset(root, prel) if root else None
        if rp is None or rp.suffix.lower() != ".png":
            raise NotOwned(f"unknown asset: {prel}")
        in_dir = Path(folder_paths.get_org_input_directory(org))
        in_dir.mkdir(parents=True, exist_ok=True)
        ref_names.append(_stage_source(in_dir, rp, "rerun"))
    ref_names.extend(_resolve_uploads(org, uploads))
    if ref_names:
        loads = sorted((nid for nid, n in graph.items()
                        if isinstance(n, dict) and n.get("class_type") == "LoadImage"),
                       key=lambda s: (len(s), s))
        if not loads:
            raise DispatchError("this workflow takes no reference images")
        for nid, name in zip(loads, ref_names):
            graph[nid]["inputs"]["image"] = name
    try:
        n_runs = max(1, min(int(count), 4))
    except (TypeError, ValueError):
        n_runs = 1
    base = out_prefix or f"reruns/{re.sub(r'[^a-zA-Z0-9_-]', '_', p.stem)[:40]}"
    # Save-outputs-to destination: an explicit Stack, or an auto-stack when the
    # org opted in and this is a batch. Routing keys on the shared base, so every
    # uniquely-tagged rerun output still lands in the one destination.
    sid = _dest_stack(org, stack, prompt=instr, multi=n_runs > 1,
                      owner=_owner_of(request), workspace=_workspace_of(request))
    ids = []
    for _ in range(n_runs):
        tag = _stamp(base)                    # each rerun owns a unique namespace
        g = _retag(_reseed(json.loads(json.dumps(graph))), tag)
        ids.append(await _dispatch(
            request, server, org, g,
            kind="rerun", prompt=instr, refs=ref_names,
            uploads=list(uploads or []), parents=[rel], output_prefix=tag))
        if sid:
            stacks_store.route_prefix(org, tag, sid, added_by=_owner_of(request))
    return {"ok": True, "prompt_ids": ids, "stack": sid}


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
        nm = _stage_source(in_dir, p, "amend")
        ref_names.append(nm)
    # de-dupe, keep order, cap at the graph's 5
    seen: set = set()
    refs = [n for n in ref_names + new_uploads if not (n in seen or seen.add(n))][:5]
    if not refs:
        raise DispatchError("no reference images available to amend")
    _cancel_job(server, org, prompt_id)
    await _cancel_activity(request, prompt_id)   # the engine-side half of the swap
    parents = list(orig.get("parents") or []) + list(paths or [])
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(refs[0]).stem)[:40] + f"_amended_{_job_tag()}"
    lessons = _lessons(org, _library_root(org), parents)
    if len(refs) == 1:
        size = _repose_size(in_dir / refs[0]) if _REPOSE.search(instruction) else None
        graph, kind, out = _fix_graph(refs[0], instruction, f"fixes/{stem}", size=size, lessons=lessons), "fix", f"fixes/{stem}"
    else:
        graph, kind, out = _compose_graph(refs, instruction, f"composes/{stem}", lessons=lessons), "compose", f"composes/{stem}"
    all_uploads = list(dict.fromkeys(list(orig.get("uploads") or []) + new_uploads))
    pid = await _dispatch(request, server, org, graph, lessons=lessons, kind=kind, prompt=instruction,
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

# Client config, injected as window.HZ before the shell loads. `pk` is a
# PUBLISHABLE key (safe in the browser) and it is the ONE credential the browser
# carries to api.hanzo.ai: the Chat sidebar streams enso from
# /v1/chat/completions with it, and the beacon attributes /v1/event with it —
# Cloud resolves the tenant server-side from the key, so no page carries a
# project key or a site id. When it is absent Chat falls back to the secure
# same-origin copilot path (httpOnly cookie) and events go unattributed.
# NEVER a server secret.
#
# PUBLISHABLE_KEY is the fleet's ONE spelling for this key -- the name says what
# the value is (`pk-`, which the allowlist below enforces). There are no
# fallbacks on purpose: three accepted names meant three places to look when the
# page shipped keyless, and the chart could satisfy a name the code did not read.
_PUBLISHABLE_KEY_ENV = ("PUBLISHABLE_KEY",)
_PUBLISHABLE_PREFIX = "pk-"  # gateway iamauth.go: the ONE client-safe key family


def _publishable(pk: str) -> str:
    """Fail CLOSED via allowlist: hand the browser a value ONLY when it is a
    publishable key (`pk-`, the single client-safe prefix the gateway defines).
    Every server secret (`hk-`/`sk-`/`fw_`/`hz_`), JWT, or unknown value is dropped
    so a misconfigured secret can never reach page source; Chat then falls back to
    the secure same-origin copilot path."""
    pk = pk.strip()
    return pk if pk.startswith(_PUBLISHABLE_PREFIX) else ""


def _hz_config_script() -> str:
    """`<script>window.HZ={...}</script>` — JSON-encoded so no value can break out
    of the tag. Reads the publishable key from env; empty when unset."""
    pk = ""
    for key in _PUBLISHABLE_KEY_ENV:
        pk = _publishable(os.environ.get(key, ""))
        if pk:
            break
    cfg = {"aiBase": (os.environ.get("STUDIO_AI_BASE", "").strip() or "https://api.hanzo.ai").rstrip("/"),
           "pk": pk,
           "model": os.environ.get("STUDIO_CHAT_MODEL", "").strip() or "enso"}
    # json.dumps escapes quotes/backslashes and (ensure_ascii) U+2028/2029; also
    # neutralize any literal "</" so no value can close the <script> tag.
    return "<script>window.HZ=" + json.dumps(cfg).replace("</", "<\\/") + ";</script>"


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
    # Rebrand the upstream editor frontend → Hanzo Studio. The Editor is a first-party
    # surface, so the upstream name stays out of the UI (the ComfyUI-format compat is
    # internal only). Cheap, idempotent string swaps on the served index (tab title +
    # the loading status a screen reader announces).
    html = html.replace("<title>ComfyUI</title>", "<title>Hanzo Studio</title>")
    html = html.replace("Loading ComfyUI...", "Loading Hanzo Studio…")
    if "/studio/shell.js" not in html:
        body = _hz_config_script() + _SHELL_BODY
        html = html.replace("</head>", _SHELL_HEAD + "</head>", 1) if "</head>" in html else _SHELL_HEAD + html
        html = html.replace("</body>", body + "</body>", 1) if "</body>" in html else html + body
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


_MAX_UPLOAD = 256 * 1024 * 1024       # 256 MB — ONE ceiling, equal to the worker mirror cap (gpu.go outputMax); holds a 4k master OR a short render's video
_UPLOAD_EXT = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".glb")
# Explicit types for served assets: the runtime's mimetypes table lacks webp, and
# octet-stream + nosniff breaks <img>/<video>/<model-viewer> rendering of references.
_CTYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".mp4": "video/mp4", ".webm": "video/webm", ".glb": "model/gltf-binary"}
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
# A file whose format-terminal marker is missing is a truncated write (a worker died
# mid-upload, a mirror was interrupted). Encoders emit these bytes LAST, so their
# presence proves the write finished; their absence on a settled file proves it never
# did. A file younger than the settle window with no proof either way may still be
# mid-write — it is judged on a later pass, never guessed at.
_SETTLE_SECS = 10


def _intact(path: Path) -> bool | None:
    """Completion evidence for a landed output. True: the terminal marker is present
    (PNG IEND / JPEG EOI / RIFF size / a consistent MP4 box chain). False: provably
    truncated. None: the format carries no terminal marker."""
    try:
        ext = path.suffix.lower()
        size = path.stat().st_size
        if size < 16:
            return False
        with path.open("rb") as f:
            head = f.read(12)
            if ext == ".png":
                if not head.startswith(b"\x89PNG\r\n\x1a\n"):
                    return False
                f.seek(size - 12)
                return b"IEND" in f.read()
            if ext in (".jpg", ".jpeg"):
                if not head.startswith(b"\xff\xd8"):
                    return False
                f.seek(size - 2)
                return f.read(2) == b"\xff\xd9"
            if ext == ".webp":
                if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
                    return False
                return int.from_bytes(head[4:8], "little") + 8 in (size, size - 1)
            if ext in (".mp4", ".m4v", ".mov"):
                off = 0
                while off < size:
                    f.seek(off)
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        return False
                    n = int.from_bytes(hdr[:4], "big")
                    if n == 0:
                        return None  # box runs to EOF — no terminal evidence
                    if n == 1:
                        big = f.read(8)
                        if len(big) < 8:
                            return False
                        n = int.from_bytes(big, "big")
                    if n < 8:
                        return False
                    off += n
                return off == size
        return None
    except OSError:
        return None


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
        whole = _intact(root / landed) if root else None
        if whole is not True and now - mt < _SETTLE_SECS:
            continue  # may still be mid-write — judged on a later pass, never guessed
        if whole is False:
            worklog.set_status(org, r["id"], "failed",
                               note="output file is truncated — an incomplete render never reads done")
            r["status"] = "failed"
            continue
        if _implausibly_fast(r, mt):
            worklog.set_status(org, r["id"], "failed",
                               note="completed implausibly fast — engine did not sample")
            r["status"] = "failed"
        else:
            worklog.mark_finished(org, r["id"], mt)
            r["status"], r["finished_at"] = "done", mt


def _resolve_outputs(org: str, root: Path | None, rows: list[dict], now: int) -> None:
    """Attach each row's OWN landed output — the ONE resolution every view and
    lineage walk uses. Runs the terminal-state mechanism first; a row that is not
    done gets no output, so a pending job can never claim an earlier run's file
    (a rejected same-stem sibling) as its result or as a lineage version."""
    _finalize_landed(org, root, rows, now)
    for r in rows:
        r["output"] = _row_output(root, r.get("output_prefix")) if r.get("status") == "done" else None


def _producer(rows: list[dict], rel: str) -> dict | None:
    """The work-log row whose landed output IS this library asset — the ONE join
    between a gallery card and the dispatch that made it. Rows must already have been
    through ``_resolve_outputs``."""
    return next((r for r in rows if r.get("output") == rel), None)


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
    _resolve_outputs(org, root, rows, int(time.time()))
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
    if _intact(root / rel) is False:
        return False  # a truncated file never enters the library
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


def _land_film(org: str, rel: str, src: Path) -> bool:
    """A rendered master or still, entering the org's library through the SAME
    ingest point every render uses — so it carries a work-log row, joins a Stack
    that was asked for, and is refused if it is truncated. The film module knows
    what it made; it does not know where an org's assets live, and this is the
    one function that does."""
    root = Path(folder_paths.get_org_output_directory(org))
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return _index_upload(org, root, rel, design=None, kind="film", role=None,
                         node="studio pod")


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

    # Office documents — the non-GPU pipeline (catalog + AI-fill + renderers) — are
    # their own orthogonal subsystem, registered here through the same injected tenancy
    # resolvers so the module stays decoupled (identical seam to stacks_store).
    from middleware.documents import add_documents_routes
    add_documents_routes(routes, server, org_of=_org_of, owner_of=_owner_of)

    # Commerce ⇄ Studio — READ the product catalog / store / listing, and publish a
    # render straight to the storefront listing karma.style serves. Same injected
    # tenancy seam; the module owns the upstream shapes, this file owns nothing of them.
    from middleware.commerce_catalog import add_commerce_routes
    add_commerce_routes(routes, org_of=_org_of)

    # Films — the six files `@hanzo/frame` resolves from one prefix, composed and
    # rendered here instead of by a Makefile in a website's repository. Same
    # injected tenancy seam, plus the ONE library ingest point so a rendered
    # master is an asset like every other thing Studio makes.
    from middleware.film import add_film_routes
    add_film_routes(routes, server, org_of=_org_of, owner_of=_owner_of, land=_land_film)

    @routes.get("/studio")
    async def studio_home(request: web.Request):
        # no-store: the home HTML changes every release; without this, browsers
        # (and any proxy) heuristically cache it and serve a STALE page after a
        # deploy — which is why UI fixes appeared "not working" to a user still
        # running yesterday's cached page. Always serve fresh; the page is small
        # and its shell.js/shell.css keep their own cache.
        html = _HTML.read_text().replace(_SHELL_BODY, _hz_config_script() + _SHELL_BODY, 1)
        return web.Response(text=html, content_type="text/html",
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
        rows = worklog.load(org)
        row = next((r for r in rows if r.get("id") == pid), None)
        if row is None:
            return web.json_response({"error": "not found"}, status=404)
        root = _library_root(org)
        # Same terminal-state mechanism as every other view; only a DONE row owns
        # a landed output — a pending row must never open an older sibling's graph.
        _finalize_landed(org, root, rows, int(time.time()))
        landed = _row_output(root, row.get("output_prefix")) if row.get("status") == "done" else None
        p = _safe_asset(root, landed) if (root and landed) else None
        if p is None or p.suffix.lower() not in _SAVE_EXT:
            return web.json_response({"error": "workflow not available yet"}, status=404)
        info = _embedded(p)   # PNG info OR video-container metadata — the ONE reader
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
            if isinstance(n, dict) and n.get("class_type") in _SAVERS:
                out_prefix = n.get("inputs", {}).get("filename_prefix", "") or out_prefix
        out_prefix = _stamp(out_prefix or f"templates/{slug}")   # unique per render (image OR video)
        sid = _dest_stack(org, body.get("stack"), prompt=rec.get("name", slug),
                          multi=_graph_is_batch(prompt), owner=_owner_of(request),
                          workspace=_workspace_of(request))
        try:
            g = _retag(_reseed(json.loads(json.dumps(prompt))), out_prefix)
            pid = await _dispatch(request, server, org, g,
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
            suf = p.suffix.lower()
            if suf == ".png" or suf in _VIDEO_EXT:
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
        suf = p.suffix.lower()
        if request.query.get("thumb") and suf in _REF_EXT:
            try:
                tdir = root / ".thumbs"
                tdir.mkdir(exist_ok=True)
                key = re.sub(r"[^a-zA-Z0-9]", "_", str(p))[-120:]
                tp = tdir / f"{key}.webp"
                if not tp.is_file() or tp.stat().st_mtime < p.stat().st_mtime:
                    from PIL import Image
                    im = Image.open(p)
                    im.thumbnail((480, 720))  # grid-sized; keeps aspect
                    # WEBP is ~30% smaller than JPEG at the same quality → less bandwidth.
                    im.convert("RGB").save(tp, "WEBP", quality=80, method=4)
                # Content-Type MUST be set explicitly: aiohttp's FileResponse guesses
                # from the extension via Python's mimetypes, which does NOT know .webp,
                # so it fell back to application/octet-stream — which the browser refuses
                # to render as an <img> (blank grid, "assets won't load").
                # Long cache (the ?v=sha URL busts on change) but REVALIDATABLE — not
                # `immutable`, which would lock a single bad response in the browser for a
                # year with no way out but a hard-refresh. A week + revalidate keeps the
                # repeat-load speed without the poisoned-cache trap.
                return web.FileResponse(tp, headers={"Content-Type": "image/webp",
                    "Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"})
            except Exception:
                pass  # fall through to full-res on any thumbnail failure
        elif request.query.get("thumb") and suf in _VIDEO_EXT:
            # A video's poster is its first frame — the SAME cache scheme as images. On ANY
            # failure return 404, NEVER the full file (falling through would serve the whole
            # mp4 as an <img>). root is the tenant library here, so it is never None.
            try:
                if root is None:
                    return web.Response(status=404)
                tdir = root / ".thumbs"
                tdir.mkdir(exist_ok=True)
                key = re.sub(r"[^a-zA-Z0-9]", "_", str(p))[-120:]
                tp = tdir / f"{key}.jpg"
                if not tp.is_file() or tp.stat().st_mtime < p.stat().st_mtime:
                    im = _poster(p)
                    im.thumbnail((480, 720))
                    im.convert("RGB").save(tp, "JPEG", quality=82)
                return web.FileResponse(tp, headers={"Content-Type": "image/jpeg",
                    "Cache-Control": "public, max-age=86400"})
            except Exception:
                return web.Response(status=404)
        elif request.query.get("thumb") and suf in _MODEL3D_EXT:
            # A mesh has no raster frame; the card renders <model-viewer> instead. 404 so
            # the thumb request never falls through and serves the whole .glb as an <img>.
            return web.Response(status=404)
        ct = _CTYPE.get(suf)
        return web.FileResponse(p, headers={"Content-Type": ct} if ct else None)

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
            return web.json_response({"error": "file name required (.png/.jpg/.jpeg/.webp/.mp4/.webm)"}, status=400)
        if name.startswith("."):
            # `._foo.png` (AppleDouble fork) and other dotfiles pass the extension
            # check but are not renders.
            return web.json_response({"error": "hidden file"}, status=400)
        # The name is rendered into gallery card HTML downstream, so <>"'& in it is
        # a stored-XSS vector. Collapse to a safe charset at the boundary (keep the
        # already-validated extension) — the pipeline's own names are safe already,
        # so this only ever touches a hostile upload name.
        _stem, _ext = os.path.splitext(name)
        name = (re.sub(r"[^A-Za-z0-9._-]", "_", _stem)[:120] or "upload") + _ext.lower()
        if not data:
            return web.json_response({"error": "empty body"}, status=400)
        if len(data) > _MAX_UPLOAD:
            return web.json_response({"error": "file too large"}, status=413)
        if not (data[:8] == b"\x89PNG\r\n\x1a\n"
                or data[:3] == b"\xff\xd8\xff"
                or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                or (len(data) > 12 and data[4:8] == b"ftyp")   # mp4/mov: brand box at OFFSET 4
                or data[:4] == b"\x1a\x45\xdf\xa3"              # webm/mkv: EBML header
                or data[:4] == b"glTF"):                        # glb: glTF binary magic
            # The name lies sometimes; the bytes never do.
            return web.json_response({"error": "not a supported media file"}, status=400)
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
        # A FIX render arriving through this door (the worker mirror) keeps its colour
        # restore — self-scoped to fixes/, fail-safe, image-only in practice.
        colormatch_fix_ingest(request, sub, name)
        # index at ingest → instantly findable in Assets + Queue & History (with node).
        # kind defaults by suffix so a video row selects the motion judge + the video card,
        # and a .glb selects the 3D (model-viewer) card.
        _suf = os.path.splitext(name)[1].lower()
        kind = request.query.get("kind") or (
            "model3d" if _suf in _MODEL3D_EXT else "video" if _suf in _VIDEO_EXT else "render")
        created = _index_upload(org, out_root, rel,
                                design=request.query.get("design") or None,
                                kind=kind,
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
        """The org's IN-FLIGHT BYO-GPU renders — read AUTHORITATIVELY from the cloud
        gpu-jobs tasks engine: each row carries the REAL claiming GPU (activity.identity)
        and REAL status, so a dispatched fix/compose/rerun is visible AND attributed to
        the machine actually running it — and nothing can render on a GPU without
        appearing here. render_jobs.json is used ONLY as a ~2s pre-claim placeholder for
        a job the engine hasn't listed yet. Terminal (done/failed/cancelled) and stale
        rows drop off. Tenant-scoped by the caller's bearer via the tasks gate."""
        org = _org_of(request)
        root = _library_root(org)
        now = int(time.time())
        # Watching the queue HEALS it: held jobs (fabric was unreachable at their
        # dispatch) re-enqueue with this caller's own credentials, then list below.
        await _retry_held(request, server, org)
        held = [{"id": r.get("id", ""), "status": "queued", "node": "waiting",
                 "age": now - int(r.get("ts", now)), "prefix": r.get("output_prefix", ""),
                 "refs": r.get("refs") or []} for r in _list_held(org)]
        acts = await _list_gpu_jobs(request)
        live, seen = [], set()
        if acts is not None:
            for a in acts:
                if _act_type_name(a) not in ("studio.render", ""):
                    continue
                job = _job_from_activity(a, now)
                if not job["id"]:
                    continue
                seen.add(job["id"])
                # Terminal rows leave the visible queue; a WAITING job stays visible
                # no matter how old — hiding queued work by age is how "queued —
                # starts when a GPU connects" silently became "vanished".
                if job["status"] in ("done", "failed", "cancelled"):
                    continue
                live.append(job)
        placeholder = _render_queue_placeholder(root, now, seen) if root is not None else []
        return web.json_response({"jobs": held + placeholder + live})

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
                old = a.get("status")
                a["status"] = status
                a["updatedAt"] = int(time.time())
                _save_library(root, lib)
                # Curation feeds the flywheel: archiving/flagging an output records it as
                # a bad response for training; restoring clears that auto-negative. Only
                # on a real change, so a no-op re-set never double-logs.
                if status != old:
                    _train_on_status(request, root, org, rel, status)
                return web.json_response({"ok": True, "path": rel, "status": status})
        return web.json_response({"error": "asset not in library"}, status=404)

    @routes.post("/v1/library/publish")
    async def publish_to_cms(request: web.Request):
        """Push ONE library asset into the Hanzo content lane (cms.hanzo.ai) as a draft
        Asset — ``content_publish``, the one writer, on demand instead of only at render
        completion. A DIFFERENT system from ``/v1/library/status`` ``published`` (that is
        this workspace's own curation state), hence its own route and its own verb.

        The upstream status and body are returned VERBATIM. A 403 means the caller's IAM
        principal holds no Content Editor role — Studio cannot fix that (the write runs as
        the caller, by design), so it is reported with its remedy and never retried, never
        degraded into a local status change."""
        body = await request.json()
        rel = body.get("path", "")
        org = _org_of(request)
        root = _library_root(org)
        p = _safe_asset(root, rel) if root is not None else None
        if p is None:
            return web.json_response({"error": "unknown asset"}, status=404)
        a = next((x for x in _load_library(root).get("assets", []) if x.get("path") == rel), {})
        rows = worklog.load(org)
        _resolve_outputs(org, root, rows, int(time.time()))
        job = _producer(rows, rel) or {}
        tags = a.get("tags") or ""
        # Exactly one artifact, in the {filename, subfolder, type} shape
        # content_publish.artifacts() reads — ORG-relative, because object_key() re-prefixes
        # orgs/<org>/output, so the record lands on the bytes the library holds. (_view_params
        # answers a different question — how /view addresses the file — and is relative to the
        # FLAT output dir in the dev layout.)
        lib_rel = p.relative_to(root.resolve())
        res = await content_publish.publish_render(
            org_id=org, prompt_id=job.get("id", ""), workflow=_asset_graph(p),
            history_result={"outputs": {"library": {"images": [
                {"filename": lib_rel.name, "type": "output",
                 "subfolder": "" if lib_rel.parent == Path(".") else str(lib_rel.parent)}]}}},
            iam_token=request.get("iam_token"),
            extra_data={"design": a.get("design"), "kind": a.get("kind"), "role": a.get("role"),
                        "prompt": job.get("prompt", ""),
                        "tags": ",".join(tags) if isinstance(tags, list) else tags},
        )
        if res["landed"]:
            return web.json_response({"ok": True, "path": rel, "status": "draft"})
        err = res["errors"][0]
        return web.json_response({"error": err["message"], "path": rel}, status=err["status"])

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
                digest = hashlib.md5()
                with p.open("rb") as fh:   # bounded chunks — a 256MB video never lands in memory
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                h = digest.hexdigest()
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
        """Re-render an asset's OWN embedded workflow — optionally with a NEW
        prompt and/or swapped reference images. Body: {path, prompt?, paths?,
        uploads?, count?, stack?}. The precise-flow contract: the SAME graph,
        exactly the changes asked for. Thin adapter over dispatch_rerun (the ONE
        implementation the chat `rerun` tool also calls)."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_rerun(
                request, server, _org_of(request), body.get("path", ""),
                prompt=body.get("prompt"), paths=body.get("paths"),
                uploads=body.get("uploads"), count=body.get("count", 1),
                stack=body.get("stack")))
        except NotOwned as e:
            return web.json_response({"error": str(e)}, status=404)
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

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
        realm = _cancel_job(server, org, pid)
        engine = await _cancel_activity(request, pid)
        return web.json_response({"ok": True, "canceled": realm, "engine": engine})

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
        # A queued job stays ACTIVE for as long as the engine will still start it
        # (the 7-day scheduleToStartTimeout) — the old 30-min window silently
        # dropped waiting work from the visible queue while it could still run.
        active = [r for r in rows if r.get("status") not in ("done", "failed", "cancelled")
                  and now - int(r.get("ts", now)) <= 604800]
        # RUNNING is a FACT, not an assumption. A gpu-lane job runs only when the
        # engine says a worker CLAIMED it (STARTED); the local lane's head runs
        # because this pod itself executes it. The old rule — "the oldest job in
        # each lane is running" — showed a ticking work-in-progress timer on jobs
        # NO worker had touched, with zero GPUs connected.
        engine_running: set = set()
        for a in (await _list_gpu_jobs(request) or []):
            if _act_type_name(a) in ("studio.render", "") and _activity_phase(a) == "running":
                j = _job_from_activity(a, now)
                if j.get("id"):
                    engine_running.add(j["id"])
        by_lane: dict[str, list[dict]] = {}
        for r in active:
            by_lane.setdefault(r.get("lane") or "local", []).append(r)
        for lane, jobs in by_lane.items():
            if lane == "local":
                head = min(jobs, key=lambda j: j.get("ts", 0))
                if not head.get("started_at"):
                    worklog.mark_started(org, head["id"], now)
                    head["started_at"] = now
                head["status"] = "running"
                continue
            for r in jobs:
                if r["id"] in engine_running:
                    if not r.get("started_at"):
                        worklog.mark_started(org, r["id"], now)
                        r["started_at"] = now
                    r["status"] = "running"
        medians = worklog.median_durations(rows)
        est, counts = worklog.estimate_lanes(active, medians, now)
        lane_live = {lane: any(j.get("status") == "running" for j in jobs)
                     for lane, jobs in by_lane.items()}
        items = {}
        for r in active:
            lane = r.get("lane") or "local"
            items[r["id"]] = {**est.get(r["id"], {}), "id": r["id"], "kind": r.get("kind", ""),
                              "prompt": r.get("prompt", ""), "status": r.get("status"),
                              "node": r.get("node"),
                              # Whether ANYTHING in this job's lane is genuinely
                              # running — a queued job in a dead lane shows plain
                              # "queued", never a fictional countdown.
                              "lane_running": lane_live.get(lane, False)}
        return web.json_response({"items": items, "medians": medians, "lanes": counts, "now": now})

    @routes.get("/v1/nodes")
    async def get_nodes(request: web.Request):
        """The caller-org's GPU nodes (online/offline) for the queue header badges, plus
        this studio pod — each annotated with LIVE queue depth read from the authoritative
        gpu-jobs activities (jobs whose claiming identity, or targeted 'gpu:<name>' lane,
        is this node). The fleet is token-scoped, so only the caller-org's nodes return."""
        try:
            from middleware.gpu_dispatch import _bearer, _fleet_machines, _node_label
        except ImportError:
            from .middleware.gpu_dispatch import _bearer, _fleet_machines, _node_label
        now = int(time.time())
        depth: dict = {}
        for a in (await _list_gpu_jobs(request) or []):
            if _act_type_name(a) not in ("studio.render", ""):
                continue
            ph = _activity_phase(a)
            if ph not in ("queued", "running"):
                continue
            d = depth.setdefault(_job_from_activity(a, now)["node"], {"queued": 0, "running": 0})
            d[ph] += 1
        nodes = []
        try:
            tok = _bearer(request)
            for m in (_fleet_machines(tok) if tok else []):
                if not m.get("gpu"):
                    continue
                name = _node_label(m)
                d = depth.get(name, {"queued": 0, "running": 0})
                nodes.append({"name": name, "online": m.get("status") == "online",
                              "queued": d["queued"], "running": d["running"],
                              "depth": d["queued"] + d["running"]})
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
        _fold_engine(org, rows, await _list_gpu_jobs(request))
        _resolve_outputs(org, root, rows, int(time.time()))
        out = [dict(r) for r in reversed(rows)]
        q = (request.query.get("q") or "").lower().strip()
        if q:
            # search prompt + kind + the render's paths (output_prefix/output) + refs, so
            # an ingested render with an empty prompt is still findable by its filename.
            out = [r for r in out if q in " ".join(str(x) for x in
                   [r.get("prompt", ""), r.get("kind", ""), r.get("output_prefix", ""),
                    r.get("output", ""), *(r.get("refs") or [])]).lower()]
        return web.json_response({"org": org, "items": out[:200]})

    @routes.post("/v1/worklog/retry")
    async def retry_worklog(request: web.Request):
        """Re-run recorded renders under TODAY's rules — the ONE replay path.

        Body: ``{ids:[...]}`` for specific rows, or ``{failed:true, limit:N}`` to replay this
        org's most recent failures (newest first, default 20, max 50). Each row re-enters the
        SAME dispatch funnel a fresh submit uses, so the new flow frames, the
        construction-lines ban and the integrity gate all apply — no replay-only code path.

        Each retry is a FULL dispatch (stage the sources, enqueue), so a large batch holds
        the connection for minutes: the UI therefore sends small ``ids`` chunks and shows
        progress. ``{failed:true}`` stays for one-shot/CLI use where a long wait is fine.

        Returns per-row outcomes: ``queued`` with the new job id, or ``refused`` with the
        reason (a row whose sources were not recorded is never approximated). The original
        rows are left as they are — history is not rewritten; the retry is a NEW job."""
        org = _org_of(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        rows = worklog.load(org)
        ids = [str(i) for i in (body.get("ids") or []) if i]
        if ids:
            wanted = [r for r in rows if str(r.get("id")) in set(ids)]
        elif body.get("failed"):
            try:
                limit = max(1, min(50, int(body.get("limit", 20))))
            except (TypeError, ValueError):
                limit = 20
            wanted = [r for r in reversed(rows) if r.get("status") == "failed"][:limit]
        else:
            return web.json_response({"error": "pass {ids:[...]} or {failed:true}"}, status=400)
        out = []
        for r in wanted:
            rid = str(r.get("id"))
            try:
                res = await retry_row(request, server, org, r)
                out.append({"id": rid, "state": "queued", "prompt_id": res.get("prompt_id"),
                            "output_prefix": res.get("output_prefix")})
            except DispatchError as e:
                out.append({"id": rid, "state": "refused", "reason": str(e)})
        queued = sum(1 for o in out if o["state"] == "queued")
        return web.json_response({"org": org, "requested": len(wanted),
                                  "queued": queued, "refused": len(out) - queued,
                                  "results": out})

    @routes.post("/v1/worklog/delete")
    async def delete_worklog(request: web.Request):
        """Remove queue/history entries by id — the user clearing render requests they
        don't want (a cancelled probe, a failed attempt, finished clutter). Deletes ONLY
        the log rows; output images live in the library and are archived/deleted there.
        Body: {id} or {ids:[...]}."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        ids = body.get("ids")
        if ids is None:
            ids = [body.get("id")] if body.get("id") else []
        org = _org_of(request)
        removed = worklog.remove(org, ids)
        return web.json_response({"ok": True, "removed": removed},
                                 headers={"Cache-Control": "no-store"})

    @routes.get("/v1/worklog/lineage")
    async def get_lineage(request: web.Request):
        """The version chain for one library asset — ancestors (what it was derived
        from, with the prompt at each step) and descendants (later fixes of it), plus
        the ``job`` row that PRODUCED it so the context panel can show real provenance
        (prompt, references, params, process trail) instead of an empty shell.
        Pure over THIS org's rows; cross-tenant lineage is impossible by construction."""
        org = _org_of(request)
        root = _library_root(org)
        target = request.query.get("path", "")
        rows = worklog.load(org)
        _resolve_outputs(org, root, rows, int(time.time()))
        return web.json_response({**worklog.build_lineage(rows, target),
                                  "job": _producer(rows, target)})

    @routes.post("/v1/library/fix")
    async def fix(request: web.Request):
        """Edit a generated gallery image. Body: {path|upload, instruction, strength?,
        seed?, mask?, stack?, kind?}. `strength` (edit-strength → denoise, default 0.25)
        is how much may change; `seed` reuses the source's original by default; `mask`
        (an uploaded painted image) restricts the edit to a region; `kind` picks a FLOWS
        lane (product / cad_ghost / …) whose frame + denoise shape the conversion.
        Always an edit of the SOURCE latent — never a text-to-image regenerate."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_fix(
                request, server, _org_of(request), body.get("path", ""),
                body.get("instruction", ""), body.get("upload"), body.get("stack"),
                strength=body.get("strength"), seed=body.get("seed"), mask=body.get("mask"),
                flow_kind=body.get("kind")))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/v1/library/image")
    async def image(request: web.Request):
        """Generate an image from {prompt, width?, height?, seed?, stack?} — the proven
        Z-Image-Turbo text-to-image graph, the composer's default create lane. Org from the
        bearer claim only; a bad request is {"error": …} 400, never a silent nothing, so
        the composer can always transition to a queued/generating state."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_image(
                request, server, _org_of(request), body.get("prompt", ""),
                width=body.get("width"), height=body.get("height"),
                seed=body.get("seed"), stack=body.get("stack"),
                flow_kind=body.get("kind")))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/v1/library/video")
    async def video(request: web.Request):
        """Generate a video from {prompt, path?, upload?, width?, height?, length?, seed?,
        stack?}. Org is the bearer-token claim (IAM middleware) ONLY — an ``org`` field in
        the body or query is never read. The proven Wan 2.2 graph, dispatched through the
        ONE funnel; a bad request surfaces as {"error": …} 400, never a silent nothing."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_video(
                request, server, _org_of(request), body.get("prompt", ""),
                rel=body.get("path"), upload=body.get("upload"),
                width=body.get("width"), height=body.get("height"),
                length=body.get("length"), seed=body.get("seed"), stack=body.get("stack")))
        except DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/v1/library/model3d")
    async def model3d(request: web.Request):
        """Generate a 3D mesh (.glb) from {path?|upload?, seed?, stack?} — ONE reference
        image required. Org from the bearer claim only. The proven Hunyuan3D-2.1 graph
        through the ONE dispatch funnel; a bad request is {"error": …} 400."""
        body = await request.json()
        try:
            return web.json_response(await dispatch_model3d(
                request, server, _org_of(request),
                rel=body.get("path"), upload=body.get("upload"),
                seed=body.get("seed"), stack=body.get("stack")))
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
                body.get("instruction", ""), body.get("uploads"), body.get("stack"),
                flow_kind=body.get("kind"), base=body.get("base"),
                strength=body.get("strength")))
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
        org = _org_of(request)
        root = _library_root(org)
        if root is None:
            return web.json_response({"error": "no library for org"}, status=404)
        patch: dict = {}
        if stars is not None:
            patch["stars"] = stars
        if "vote" in body:
            patch["vote"] = _norm_vote(body.get("vote"))   # 👍 1 · 👎 -1 · cleared 0
        if "notes" in body:
            patch["notes"] = str(body.get("notes") or "")[:2000]
        if "path" in body:
            # The run's landed output, stored beside the rating so the flywheel row
            # carries {path, stars, note, ts} keyed by run id — the AI can load the
            # image it's being told to do differently next time.
            patch["path"] = str(body.get("path") or "")[:512]
        if ("vote" in body) or ("notes" in body):
            patch["archived"] = False   # an explicit judgement is user-owned, not the auto archive-negative
        entry = _rate_write(root, key, patch)   # ONE atomic writer, shared with the archive hook
        # Every judgement (vote / stars / text) is also logged, immutable, to the
        # training-feedback dataset the model trainer consumes.
        if ("vote" in body) or ("notes" in body) or (stars is not None):
            _append_feedback(request, root, org, key, entry)
        return web.json_response({"ok": True, "key": key, "entry": entry})

    @routes.post("/v1/library/critique")
    async def critique(request: web.Request):
        """A conversation ABOUT one generated image, for critiquing the AI's work on
        it. Unlike the node-editor copilot (/v1/copilot/chat, graph-only), this loads
        the actual image so the reply is grounded in what was rendered — and EVERY
        user turn is recorded as a training lesson on this image's lineage (the same
        notes→_mistakes→_lessons flywheel a 💬 note feeds), so critiquing here
        conditions the next generation. Body: {path, message}. Reply: {reply, thread}."""
        body = await request.json()
        rel = (body.get("path") or "").strip()
        msg = (body.get("message") or "").strip()
        if not rel or not msg:
            return web.json_response({"error": "path and message required"}, status=400)
        org = _org_of(request)
        root = _library_root(org)
        p = _safe_asset(root, rel) if root else None
        if p is None:
            return web.json_response({"error": "unknown asset"}, status=404)
        # The running thread lives on the image's rating row.
        rf = root / "ratings.json"
        try:
            data = json.loads(rf.read_text())
        except Exception:
            data = {}
        entry = data.get(rel, {})
        thread = entry.get("thread") or []
        thread.append({"role": "user", "content": msg[:2000], "ts": int(time.time())})

        # Ask a vision model, grounded in the actual image + the critique so far.
        from middleware.gpu_dispatch import CLOUD, _bearer
        from middleware import quality_gate as qg
        tok = _bearer(request)
        b64 = qg._downscale(p)
        reply = ""
        if b64:
            sys_prompt = ("You are the studio's critique copilot. The user is critiquing a "
                          "generated image to improve FUTURE generations of this design. Look at "
                          "the image, acknowledge the specific flaw they point out, and restate it "
                          "as a precise, actionable correction the image model should apply next "
                          "time (color, likeness, pose, lighting, anatomy, fabric, framing). Be "
                          "concise — two sentences. Do not apologize; do not offer to regenerate.")
            content = [{"type": "text", "text": sys_prompt}]
            for t in thread[-8:]:
                content.append({"type": "text", "text": f"{t['role'].upper()}: {t['content']}"})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + b64}})
            payload = {"model": os.environ.get("STUDIO_GATE_MODEL", "").strip() or "zen-vl",
                       "messages": [{"role": "user", "content": content}],
                       "temperature": 0.2, "max_tokens": 400}
            headers = {"Content-Type": "application/json"}
            if tok:
                headers["Authorization"] = tok
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                    async with s.post(f"{CLOUD}/v1/chat/completions", json=payload, headers=headers) as r:
                        if r.status == 200:
                            reply = qg._reply_text(await r.json()).strip()
            except Exception as e:
                log.warning("critique: judge unreachable: %s", e)
        if not reply:
            reply = "Noted — I've recorded this so the next render avoids it."
        thread.append({"role": "assistant", "content": reply, "ts": int(time.time())})

        # The flywheel: the user's critique points become the note _mistakes reads,
        # so the next fix/compose over this lineage is conditioned to avoid them.
        crit = " ".join(t["content"] for t in thread if t["role"] == "user")[-2000:]
        entry["thread"] = thread[-40:]
        entry["notes"] = crit
        entry["path"] = rel
        entry["updatedAt"] = int(time.time())
        data[rel] = entry
        tmp = root / "ratings.json.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(rf)
        _append_feedback(request, root, org, rel, entry)
        return web.json_response({"ok": True, "reply": reply, "thread": thread})

    @routes.get("/v1/library/critique")
    async def critique_thread(request: web.Request):
        """The saved critique thread for one image (to reopen the conversation)."""
        org = _org_of(request)
        root = _library_root(org)
        rel = (request.query.get("path") or "").strip()
        if root is None or not rel:
            return web.json_response({"thread": []})
        try:
            data = json.loads((root / "ratings.json").read_text())
        except Exception:
            data = {}
        return web.json_response({"thread": (data.get(rel, {}) or {}).get("thread") or []})

    @routes.get("/v1/feedback/stats")
    async def feedback_stats(request: web.Request):
        """👍/👎/with-text/total counts for this org's training-feedback dataset."""
        root = _library_root(_org_of(request))
        up = down = withtext = total = 0
        if root is not None:
            try:
                for line in (root / "feedback.jsonl").read_text().splitlines():
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    total += 1
                    up += r.get("vote") == 1
                    down += r.get("vote") == -1
                    withtext += bool(r.get("text"))
            except Exception:
                pass
        return web.json_response({"up": up, "down": down, "withText": withtext, "total": total})

    @routes.get("/v1/feedback/export")
    async def feedback_export(request: web.Request):
        """This org's append-only training-feedback dataset (JSONL): every vote / stars
        / text event paired with the output it judged. What the model trainer pulls to
        build preference, reward, and instruction-tuning data — the output PNG each row
        anchors to carries its own prompt + refs in its embedded graph."""
        root = _library_root(_org_of(request))
        text = ""
        if root is not None:
            try:
                text = (root / "feedback.jsonl").read_text()
            except Exception:
                text = ""
        return web.Response(text=text, content_type="application/x-ndjson",
                            headers={"Content-Disposition": "attachment; filename=feedback.jsonl",
                                     "Cache-Control": "no-store"})

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
