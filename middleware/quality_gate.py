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
    has_fail = bool(_FAIL_RE.search(reply or ""))
    has_pass = bool(_PASS_RE.search(reply or ""))
    if has_fail and not has_pass:
        return "fail"
    if has_pass and not has_fail:
        return "pass"
    return "inconclusive"


def _downscale(path: Path) -> str | None:
    """The stored render as a ~512px JPEG, base64 — small enough for a fast vision
    call. Returns None if the image can't be read (→ the gate fails open)."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _reply_text(data: dict) -> str:
    """The assistant text from an OpenAI-style completion, tolerating a content
    string or the content-parts array some gateways return."""
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


async def judge(image_b64: str, bearer: str) -> str | None:
    """Ask the vision judge to score one image. Returns its reply text, or None on
    ANY error (non-200, bad JSON, timeout, unreachable) — the caller fails open."""
    payload = {
        "model": _model(),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
        ]}],
        "temperature": 0,
        "max_tokens": 8,
    }
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = bearer
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
            async with s.post(_url(), json=payload, headers=headers) as r:
                if r.status != 200:
                    log.info("gate: judge HTTP %s", r.status)
                    return None
                return _reply_text(await r.json())
    except Exception as e:
        log.info("gate: judge unreachable: %s", e)
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


async def run(org: str, root: Path, rel: str, bearer: str) -> None:
    """Judge one just-stored render and flag it on a clear FAIL. Never raises — a
    background task that fails open on everything else."""
    try:
        p = Path(root) / rel
        if not p.is_file():
            return
        image_b64 = _downscale(p)
        if image_b64 is None:
            log.info("gate: unreadable %s/%s — pass (fail-open)", org, rel)
            return
        reply = await judge(image_b64, bearer)
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
