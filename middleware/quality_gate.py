"""Render quality gate — the ONE guard that keeps corrupted renders out of the library.

Every render lands through ``POST /v1/library/upload`` → ``studio_home._index_upload``.
The moment a NEW asset row is created there, this fires an out-of-band judgment
(never blocks the upload response): downscale the just-stored image, ask a cloud
vision model to answer PASS/FAIL, and on a clear FAIL move the asset's library
status ``draft → flagged`` so it drops out of every default grid — the same
visibility semantics as ``deleted``, but recoverable from the UI's Flagged view.

It FAILS OPEN by construction. A PASS, an inconclusive reply, a 402/429, a timeout,
an unreachable judge, an unreadable image — anything that is not an unambiguous
FAIL — leaves the render a normal ``draft``. The gate can only ever hide clearly
broken work; it can never hide good work by accident, and it never blocks a render
from landing.

Env (all optional):
    STUDIO_GATE        "off"/"0"/"false"/"no" disables the gate entirely. Default on.
    STUDIO_GATE_MODEL  vision model id. Default "zen-vl" (the cloud's VL model).
    STUDIO_GATE_URL    full chat-completions URL override, e.g. a local engine at
                       http://127.0.0.1:1234/v1/chat/completions. Default = the
                       cloud gateway (gpu_dispatch.CLOUD) + /v1/chat/completions.

One auth path: the judgment carries the SAME bearer the upload carried, so the
cloud derives org + billing from it — no shared keys, tenant-isolated like dispatch.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import time
from pathlib import Path

import aiohttp

from middleware import worklog
from middleware.gpu_dispatch import CLOUD, _bearer

log = logging.getLogger("studio.gate")

# The strict binary judge. The model is told to answer EXACTLY PASS or FAIL; the
# failure classes are the observed corruption modes of the render pipeline.
PROMPT = (
    "You are a render QA gate for fashion imagery. Look at the image. Answer exactly "
    "PASS or FAIL. FAIL if: skin texture is corrupted (mottled, blotchy, speckled, "
    "noise-stippled), the image has ghosting/double-exposure, a frame-within-frame/"
    "poster border, or glyph/pixel-garbage regions. Otherwise PASS."
)

# The motion judge — the SAME strict binary contract as PROMPT, but the failure classes
# are the observed corruption modes of the video pipeline (it is shown start/mid/end frames).
MOTION = (
    "You are a render QA gate for generated video, shown frames sampled across its "
    "duration. Answer exactly PASS or FAIL. FAIL if: frame-to-frame flicker, "
    "identity/clothing morphing between frames, frozen/static motion where movement is "
    "expected, temporal ghosting or trails, or glyph/pixel-garbage regions. Otherwise PASS."
)

_OFF = ("off", "0", "false", "no")
_FAIL_RE = re.compile(r"\bfail\b", re.I)
_PASS_RE = re.compile(r"\bpass\b", re.I)

# Keep a reference to in-flight tasks so a fire-and-forget judgment is not
# garbage-collected before it runs (per asyncio.ensure_future docs).
_TASKS: set = set()


def enabled() -> bool:
    return os.environ.get("STUDIO_GATE", "").strip().lower() not in _OFF


def _model() -> str:
    return os.environ.get("STUDIO_GATE_MODEL", "").strip() or "zen-vl"


def _url() -> str:
    return os.environ.get("STUDIO_GATE_URL", "").strip() or (CLOUD + "/v1/chat/completions")


def parse_verdict(reply: str) -> str:
    """"pass" | "fail" | "inconclusive" from the judge's reply. Case-insensitive
    PASS/FAIL as a whole token (so "FAIL — mottled skin" reads as fail but "failure"
    does not spuriously flag). A reply carrying BOTH tokens, or NEITHER, is
    inconclusive — and inconclusive fails open."""
    fails = [m.start() for m in _FAIL_RE.finditer(reply or "")]
    passes = [m.start() for m in _PASS_RE.finditer(reply or "")]
    # The LAST token wins: a reasoning judge weighs both words before concluding
    # ("is this PASS or FAIL... verdict: FAIL") — co-occurrence is deliberation,
    # position is the verdict.
    if fails and (not passes or fails[-1] > passes[-1]):
        return "fail"
    if passes and (not fails or passes[-1] > fails[-1]):
        return "pass"
    return "inconclusive"


def _encode(im) -> str | None:
    """A PIL image as a ~512px JPEG, base64 — the ONE frame-encode contract, small
    enough for a fast vision call. Returns None on any failure."""
    try:
        im = im.convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _downscale(path: Path) -> str | None:
    """The stored image as a ~512px JPEG, base64. None if it can't be read (→ fail open)."""
    try:
        from PIL import Image
        return _encode(Image.open(path))
    except Exception:
        return None


def _video_frame(path: str, t: float) -> str | None:
    """One ~512px JPEG (base64) decoded at ``t`` seconds — seek then take the next frame.
    A fresh container per frame keeps the decode robust (no abandoned-generator state)."""
    try:
        import av
        with av.open(path) as c:
            st = c.streams.video[0]
            if t > 0 and st.time_base:
                c.seek(int(t / st.time_base), stream=st)
            for frame in c.decode(st):
                return _encode(frame.to_image())
    except Exception:
        return None
    return None


def _frames(path: Path) -> list[str] | None:
    """Up to three ~512px JPEG frames (base64) to judge, or None when unreadable. A video
    (.mp4/.webm) is sampled at start / middle / end so the motion judge sees temporal
    coherence; any other file is the single downscaled image. None → the caller decides
    (an image fails open; a video is marked ungated, never a silent pass)."""
    if path.suffix.lower() in (".mp4", ".webm"):
        try:
            import av
            with av.open(str(path)) as probe:
                st = probe.streams.video[0]
                if st.duration and st.time_base:
                    dur = float(st.duration * st.time_base)
                elif probe.duration:
                    dur = float(probe.duration) / 1_000_000
                else:
                    dur = 0.0
            targets = [0.0, dur / 2, dur] if dur > 0 else [0.0]
            out = [b for b in (_video_frame(str(path), t) for t in targets) if b]
            return out or None
        except Exception:
            return None
    b = _downscale(path)
    return [b] if b else None


def _reply_text(data: dict) -> str:
    """The assistant text from an OpenAI-style completion, tolerating a content
    string, the content-parts array some gateways return, and REASONING models
    whose verdict lands in reasoning_content while content comes back null (a
    tiny max_tokens made zen-vl spend the whole budget thinking — every verdict
    parsed empty and the gate failed open, silently passing corrupted renders)."""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content).strip():
        content = msg.get("reasoning_content") or ""
    return str(content)


async def judge(images: list[str], bearer: str, prompt: str) -> str | None:
    """Ask the vision judge to score N frames against ``prompt`` (PROMPT for a still image,
    MOTION for a video's sampled frames) — one text part + N image parts. Returns its reply
    text, or None on ANY error (non-200, bad JSON, timeout, unreachable) — caller fails open."""
    content = [{"type": "text", "text": prompt}]
    for b64 in images:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
    payload = {
        "model": _model(),
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 768,   # reasoning models think before the verdict — 8 starved them into fail-open
    }
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = bearer
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
            async with s.post(_url(), json=payload, headers=headers) as r:
                if r.status != 200:
                    log.warning("gate: judge HTTP %s", r.status)
                    return None
                return _reply_text(await r.json())
    except Exception as e:
        log.warning("gate: judge unreachable: %s", e)
        return None


def _flag(org: str, root: Path, rel: str, reply: str) -> bool:
    """Move a still-``draft`` asset to ``flagged`` and record it on its work-log row.
    Only ``draft`` transitions — a late verdict must never yank a render a human has
    since approved/published (or one already flagged/deleted). One writer of
    library.json (studio_home's helpers). Returns True if the asset was flagged."""
    from middleware import studio_home as sh  # lazy: avoid an import cycle
    lib = sh._load_library(root)
    for a in lib.get("assets", []):
        if a.get("path") == rel and a.get("status") == "draft":
            a["status"] = "flagged"
            a["updatedAt"] = int(time.time())
            sh._save_library(root, lib)
            worklog.add_event(org, rel, "flagged",
                              "quality gate: " + (reply or "").strip()[:80])
            log.info("gate: FLAGGED %s/%s", org, rel)
            return True
    return False


def _skip(org: str, root: Path, rel: str) -> None:
    """A video whose frames could not be sampled is marked visibly UNGATED — NEVER a
    silent pass: set the asset row's ``gate`` field to ``skipped`` (the card renders an
    'ungated' badge from it) and log an ``ungated`` work-log event. Same single-writer
    library pattern as ``_flag``; leaves status untouched."""
    from middleware import studio_home as sh  # lazy: avoid an import cycle
    lib = sh._load_library(root)
    hit = False
    for a in lib.get("assets", []):
        if a.get("path") == rel:
            a["gate"] = "skipped"
            a["updatedAt"] = int(time.time())
            hit = True
    if hit:
        sh._save_library(root, lib)
    worklog.add_event(org, rel, "ungated", "gate could not sample frames")
    log.info("gate: UNGATED %s/%s", org, rel)


async def run(org: str, root: Path, rel: str, bearer: str) -> None:
    """Judge one just-stored render — a still image against PROMPT, a video against MOTION
    over its sampled frames — and flag it on a clear FAIL. A video whose frames can't be
    sampled is marked UNGATED (never a silent pass); an unreadable image fails open. Never
    raises — a background task that fails open on everything else."""
    try:
        p = Path(root) / rel
        if not p.is_file():
            return
        video = p.suffix.lower() in (".mp4", ".webm")
        frames = _frames(p)
        if frames is None:
            if video:
                _skip(org, root, rel)   # honest: an ungated video is badged, never passed silently
            else:
                log.info("gate: unreadable %s/%s — pass (fail-open)", org, rel)
            return
        reply = await judge(frames, bearer, MOTION if video else PROMPT)
        verdict = parse_verdict(reply or "")
        if verdict == "fail":
            _flag(org, root, rel, reply or "")
        else:
            log.info("gate: %s %s/%s", verdict, org, rel)
    except Exception as e:  # a background task must never surface an exception
        log.info("gate: error %s/%s: %s — pass (fail-open)", org, rel, e)


def schedule(request, org: str, root: Path, rel: str) -> None:
    """Fire the judgment out-of-band. Captures the caller's bearer NOW (the request
    is gone by the time the task runs) and schedules ``run`` on the event loop.
    Never blocks the upload response; never raises."""
    if not enabled():
        return
    try:
        bearer = _bearer(request)
        task = asyncio.ensure_future(run(org, root, rel, bearer))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception as e:
        log.info("gate: schedule failed %s/%s: %s", org, rel, e)
