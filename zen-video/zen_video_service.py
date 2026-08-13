#!/usr/bin/env python3
"""zen-video: headless text-to-video queue service (Zen video family) on the GB10.

Public model id: zen3-video. Backend: Hanzo Studio (ComfyUI) on :8188 running the
Zen video diffusion model. The upstream/private model filenames live only inside
zen_wan_workflow.py and are NEVER returned to callers.

One GPU => one job at a time. A single asyncio worker drains an ordered queue and
submits to Studio, which itself serializes on the GPU. Jobs persist in SQLite so a
service restart never loses in-flight/finished job state.

Delivery: S3 (SeaweedFS) when ZEN_S3_* is configured, else the finished mp4 is
served directly from this service over the Cloudflare tunnel at
/v1/videos/files/{name} (a scoped, unguessable URL).

Billing: identity is injected by the gateway as X-User-Id / X-Org-Id (from the
IAM JWT). Every completed job writes a usage row and, if ZEN_METER_URL is set,
posts a metering event. Requests without an org identity are refused unless
ZEN_ALLOW_ANON=1 (dev only) — video is expensive, never free by accident.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from zen_wan_workflow import build_prompt

# ---------------------------------------------------------------------------
# Config (env, matches studio.service conventions)
# ---------------------------------------------------------------------------
COMFY_URL = os.environ.get("ZEN_COMFY_URL", "http://127.0.0.1:8188")
PORT = int(os.environ.get("ZEN_VIDEO_PORT", "8189"))
OUTPUT_DIR = Path(os.environ.get("ZEN_OUTPUT_DIR", "/home/z/work/hanzo/studio/output"))
DB_PATH = os.environ.get("ZEN_DB_PATH", "/home/z/zen-video/jobs.db")
PUBLIC_BASE = os.environ.get("ZEN_PUBLIC_BASE", "").rstrip("/")  # e.g. https://spark-video.hanzo.ai
ALLOW_ANON = os.environ.get("ZEN_ALLOW_ANON", "0") == "1"
METER_URL = os.environ.get("ZEN_METER_URL", "").rstrip("/")
# Bearer key that the cloud LLM gateway (hanzoai/ai) presents on the do-ai-compatible
# async video API (/v1/videos*). Provisioned in KMS, set as the "spark-video" provider
# apiKey in the ai model registry. Empty in dev => async API is open (dev only).
PROVIDER_KEY = os.environ.get("ZEN_PROVIDER_KEY", "")
POLL_SECS = float(os.environ.get("ZEN_POLL_SECS", "2"))
JOB_TIMEOUT = float(os.environ.get("ZEN_JOB_TIMEOUT", "3600"))

# S3 (optional durable delivery)
S3_ENDPOINT = os.environ.get("ZEN_S3_ENDPOINT", "")
S3_EXTERNAL = os.environ.get("ZEN_S3_EXTERNAL", "").rstrip("/")
S3_BUCKET = os.environ.get("ZEN_S3_BUCKET", "videos")
S3_KEY = os.environ.get("ZEN_S3_ACCESS_KEY", "")
S3_SECRET = os.environ.get("ZEN_S3_SECRET_KEY", "")
S3_REGION = os.environ.get("ZEN_S3_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Public model registry — Zen names only. Maps to backend generation params.
# NEVER surface the private model filename to a caller.
# ---------------------------------------------------------------------------
MODELS = {
    "zen3-video": {
        "default_size": (1280, 704),   # native 720p
        "max_seconds": 5,
        "fps": 24,
        "steps": 20,
        "cfg": 5.0,
        "shift": 8.0,
        # metered price (USD) — flat per generated second; video is expensive.
        "usd_per_second": 0.20,
    },
    # fast/preview SKU: lower res + fewer frames, cheaper
    "zen3-video-fast": {
        "default_size": (704, 704),
        "max_seconds": 3,
        "fps": 24,
        "steps": 16,
        "cfg": 5.0,
        "shift": 8.0,
        "usd_per_second": 0.08,
    },
    "zen3-video-pro": {
        "default_size": (1280, 704),
        "max_seconds": 5,
        "fps": 24,
        "steps": 30,
        "cfg": 5.0,
        "shift": 8.0,
        "usd_per_second": 0.40,
    },
    # do-ai upstream id: the cloud LLM gateway maps zen3-video -> this upstream
    # model on the async video provider. Default kept moderate so a generation
    # completes inside the gateway client's 300s ceiling.
    "wan2-2-t2v-a14b": {
        "default_size": (704, 704),
        "max_seconds": 3,
        "fps": 24,
        "steps": 20,
        "cfg": 5.0,
        "shift": 8.0,
        "usd_per_second": 0.20,
    },
}

# do-ai async status = OpenAI Sora-style. Map internal job status to it.
_DOAI_STATUS = {
    "queued": "queued", "running": "in_progress",
    "completed": "completed", "failed": "failed",
}

# ---------------------------------------------------------------------------
# SQLite job store
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT, prompt TEXT, params TEXT,
                org_id TEXT, user_id TEXT,
                prompt_id TEXT,
                url TEXT, filename TEXT,
                seconds REAL, width INTEGER, height INTEGER,
                error TEXT,
                usd REAL DEFAULT 0,
                created REAL, started REAL, finished REAL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, org_id TEXT, user_id TEXT, model TEXT,
                seconds REAL, usd REAL, ts REAL, metered INTEGER DEFAULT 0
            )"""
        )


def job_row(job_id: str):
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def job_update(job_id: str, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), job_id))


# ---------------------------------------------------------------------------
# Serialization view for API responses (Zen surface — never leak backend)
# ---------------------------------------------------------------------------
def public_job(r: dict) -> dict:
    out = {
        "id": r["id"],
        "object": "video.generation",
        "model": r["model"],
        "status": r["status"],
        "created": r.get("created"),
    }
    if r.get("seconds"):
        out["seconds"] = r["seconds"]
    if r.get("width") and r.get("height"):
        out["size"] = f"{r['width']}x{r['height']}"
    if r["status"] == "completed" and r.get("url"):
        out["url"] = r["url"]
        out["data"] = [{"url": r["url"]}]
    if r["status"] == "failed" and r.get("error"):
        out["error"] = {"message": r["error"]}
    return out


# ---------------------------------------------------------------------------
# ComfyUI interaction (async)
# ---------------------------------------------------------------------------
async def comfy_submit(session: aiohttp.ClientSession, graph: dict) -> str:
    async with session.post(f"{COMFY_URL}/prompt",
                            json={"prompt": graph, "client_id": "zen-video"},
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        return (await r.json())["prompt_id"]


async def comfy_wait(session: aiohttp.ClientSession, prompt_id: str) -> dict:
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        try:
            async with session.get(f"{COMFY_URL}/history/{prompt_id}",
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                hist = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(POLL_SECS)
            continue
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "success" or status.get("completed"):
                return entry
            if status.get("status_str") == "error":
                msgs = []
                for m in status.get("messages", []):
                    if m and m[0] == "execution_error":
                        msgs.append(json.dumps(m[1])[:500])
                raise RuntimeError("studio error: " + ("; ".join(msgs) or "unknown"))
        await asyncio.sleep(POLL_SECS)
    raise TimeoutError(f"job exceeded {JOB_TIMEOUT}s")


def find_output_mp4(entry: dict) -> tuple[str, Path]:
    """Return (filename, absolute path) of the SaveVideo mp4 from a history entry."""
    for node_out in entry.get("outputs", {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key, []) or []:
                fn = item.get("filename", "")
                if fn.endswith(".mp4"):
                    sub = item.get("subfolder", "")
                    p = OUTPUT_DIR / sub / fn
                    return fn, p
    raise RuntimeError("no mp4 in studio output")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def upload_s3(path: Path, key: str) -> str | None:
    if not (S3_ENDPOINT and S3_KEY and S3_SECRET):
        return None
    import boto3
    from botocore.config import Config
    s3 = boto3.client(
        "s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET, region_name=S3_REGION,
        config=Config(s3={"addressing_style": "path"}),
    )
    s3.upload_file(str(path), S3_BUCKET, key,
                   ExtraArgs={"ContentType": "video/mp4", "ACL": "public-read"})
    base = S3_EXTERNAL or S3_ENDPOINT
    return f"{base}/{S3_BUCKET}/{key}"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
async def worker(app: web.Application) -> None:
    q: asyncio.Queue = app["queue"]
    async with aiohttp.ClientSession() as session:
        while True:
            job_id = await q.get()
            try:
                await run_job(session, job_id)
            except Exception as e:  # never let the worker die
                job_update(job_id, status="failed", error=str(e)[:1000],
                           finished=time.time())
            finally:
                q.task_done()


async def run_job(session: aiohttp.ClientSession, job_id: str) -> None:
    r = job_row(job_id)
    if not r or r["status"] in ("completed", "failed"):
        return
    params = json.loads(r["params"])
    job_update(job_id, status="running", started=time.time())
    graph, _seed = build_prompt(**params["wan"])
    prompt_id = await comfy_submit(session, graph)
    job_update(job_id, prompt_id=prompt_id)
    entry = await comfy_wait(session, prompt_id)
    fn, path = find_output_mp4(entry)
    # deliver
    key = f"{time.strftime('%Y/%m/%d')}/{job_id}.mp4"
    url = None
    try:
        url = await asyncio.get_event_loop().run_in_executor(None, upload_s3, path, key)
    except Exception:
        url = None  # fall back to tunnel-serve
    if not url:
        url = f"{PUBLIC_BASE}/v1/videos/files/{fn}" if PUBLIC_BASE else f"/v1/videos/files/{fn}"
    # billing
    seconds = params["seconds"]
    usd = params["usd"]
    _record_usage(job_id, r["org_id"], r["user_id"], r["model"], seconds, usd)
    job_update(job_id, status="completed", url=url, filename=fn, usd=usd,
               finished=time.time())


def _record_usage(job_id, org, user, model, seconds, usd) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO usage(job_id,org_id,user_id,model,seconds,usd,ts,metered) "
            "VALUES(?,?,?,?,?,?,?,0)",
            (job_id, org, user, model, seconds, usd, time.time()),
        )
    if METER_URL and org:
        # best-effort synchronous meter post (small); failures leave metered=0 for retry
        try:
            import urllib.request
            body = json.dumps({
                "org_id": org, "user_id": user, "model": model,
                "quantity": seconds, "unit": "second", "usd": usd,
                "resource": "video.generation", "job_id": job_id,
            }).encode()
            req = urllib.request.Request(METER_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            with db() as c:
                c.execute("UPDATE usage SET metered=1 WHERE job_id=?", (job_id,))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
def _identity(request: web.Request) -> tuple[str, str]:
    return (request.headers.get("X-Org-Id", ""), request.headers.get("X-User-Id", ""))


def _bearer_ok(request: web.Request) -> bool:
    if not PROVIDER_KEY:
        return True  # dev: open async API
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:].strip() == PROVIDER_KEY


class BadRequest(Exception):
    pass


async def _enqueue(app, *, model, prompt, size, seconds, seed, negative,
                   org, user, idem_id) -> dict:
    """Validate + persist + enqueue a job. Returns the job row. Raises BadRequest."""
    spec = MODELS.get(model)
    if not spec:
        raise BadRequest(f"unknown model '{model}'")
    prompt = (prompt or "").strip()
    if not prompt:
        raise BadRequest("prompt required")
    if size:
        try:
            w, h = (int(x) for x in str(size).lower().split("x"))
        except Exception:
            raise BadRequest("size must be WxH")
    else:
        w, h = spec["default_size"]
    fps = spec["fps"]
    seconds = float(seconds) if seconds else spec["max_seconds"]
    seconds = max(1.0, min(seconds, spec["max_seconds"]))
    length = int(round(seconds * fps))
    length = ((length - 1) // 4) * 4 + 1
    seconds = round(length / fps, 3)
    usd = round(seconds * spec["usd_per_second"], 4)

    job_id = idem_id or str(uuid.uuid4())
    existing = job_row(job_id)
    if existing:
        return existing

    wan = {"positive": prompt, "width": w, "height": h, "length": length,
           "steps": spec["steps"], "cfg": spec["cfg"], "shift": spec["shift"],
           "fps": fps, "filename_prefix": f"video/zen-{job_id[:8]}"}
    if negative:
        wan["negative"] = negative
    if seed is not None:
        wan["seed"] = int(seed)
    params = {"wan": wan, "seconds": seconds, "usd": usd}
    now = time.time()
    with db() as c:
        c.execute(
            "INSERT INTO jobs(id,status,model,prompt,params,org_id,user_id,"
            "seconds,width,height,usd,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "queued", model, prompt, json.dumps(params), org, user,
             seconds, w, h, usd, now))
    await app["queue"].put(job_id)
    return job_row(job_id)


async def create_generation(request: web.Request) -> web.Response:
    org, user = _identity(request)
    if not org and not ALLOW_ANON:
        return web.json_response(
            {"error": {"message": "org identity required (gateway must inject X-Org-Id)"}},
            status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)

    idem = body.get("id") or request.headers.get("Idempotency-Key")
    try:
        r = await _enqueue(
            request.app, model=body.get("model", "zen3-video"),
            prompt=body.get("prompt"), size=body.get("size"),
            seconds=body.get("seconds"), seed=body.get("seed"),
            negative=body.get("negative_prompt"), org=org, user=user, idem_id=idem)
    except BadRequest as e:
        return web.json_response(
            {"error": {"message": str(e), "available": list(MODELS)}}, status=400)
    resp = public_job(r)
    resp["queue_position"] = request.app["queue"].qsize()
    return web.json_response(resp, status=202)


# ---------------------------------------------------------------------------
# do-ai-compatible async video API (OpenAI Sora-style) — driven by the cloud
# LLM gateway's GenerateVideoDOAI client. Auth = Bearer provider key. Identity
# and billing are owned upstream by the LLM gateway; this side just generates.
# ---------------------------------------------------------------------------
def _doai_record(r: dict) -> dict:
    out = {"id": f"video_{r['id']}", "object": "video", "model": r["model"],
           "status": _DOAI_STATUS.get(r["status"], "queued")}
    if r["status"] == "failed":
        out["error"] = {"message": r.get("error") or "generation failed",
                        "type": "generation_error"}
    else:
        out["error"] = None
    return out


def _strip_vid(vid: str) -> str:
    return vid[6:] if vid.startswith("video_") else vid


async def doai_create(request: web.Request) -> web.Response:
    if not _bearer_ok(request):
        return web.json_response({"error": {"message": "unauthorized"}}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "invalid JSON"}}, status=400)
    try:
        r = await _enqueue(
            request.app, model=body.get("model", "wan2-2-t2v-a14b"),
            prompt=body.get("prompt"), size=body.get("size"),
            seconds=body.get("seconds"), seed=body.get("seed"),
            negative=body.get("negative_prompt"),
            org=request.headers.get("X-Org-Id", ""),
            user=request.headers.get("X-User-Id", ""), idem_id=None)
    except BadRequest as e:
        return web.json_response({"error": {"message": str(e)}}, status=400)
    return web.json_response(_doai_record(r), status=202)


async def doai_status(request: web.Request) -> web.Response:
    if not _bearer_ok(request):
        return web.json_response({"error": {"message": "unauthorized"}}, status=401)
    r = job_row(_strip_vid(request.match_info["id"]))
    if not r:
        return web.json_response({"error": {"message": "not found"}}, status=404)
    return web.json_response(_doai_record(r))


async def doai_content(request: web.Request) -> web.StreamResponse:
    if not _bearer_ok(request):
        raise web.HTTPUnauthorized()
    r = job_row(_strip_vid(request.match_info["id"]))
    if not r:
        raise web.HTTPNotFound()
    if r["status"] != "completed" or not r.get("filename"):
        # do-ai returns a tiny JSON placeholder while in progress; the client
        # only downloads after polling to completion.
        return web.json_response({"status": _DOAI_STATUS.get(r["status"], "queued")},
                                 status=409)
    for p in OUTPUT_DIR.rglob(r["filename"]):
        if p.is_file():
            return web.FileResponse(p, headers={"Content-Type": "video/mp4"})
    raise web.HTTPNotFound()


async def get_generation(request: web.Request) -> web.Response:
    r = job_row(request.match_info["id"])
    if not r:
        return web.json_response({"error": {"message": "not found"}}, status=404)
    return web.json_response(public_job(r))


async def serve_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if "/" in name or ".." in name:
        raise web.HTTPBadRequest()
    # search output tree for the file
    for p in OUTPUT_DIR.rglob(name):
        if p.is_file():
            return web.FileResponse(p, headers={"Content-Type": "video/mp4"})
    raise web.HTTPNotFound()


async def healthz(request: web.Request) -> web.Response:
    comfy = "down"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{COMFY_URL}/system_stats",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    comfy = "up"
    except Exception:
        pass
    return web.json_response({
        "status": "ok", "comfy": comfy,
        "queue_depth": request.app["queue"].qsize(),
        "models": list(MODELS),
    })


async def list_models(request: web.Request) -> web.Response:
    data = [{"id": m, "object": "model", "owned_by": "zenlm",
             "max_seconds": s["max_seconds"], "default_size": f"{s['default_size'][0]}x{s['default_size'][1]}"}
            for m, s in MODELS.items()]
    return web.json_response({"object": "list", "data": data})


def make_app() -> web.Application:
    init_db()
    app = web.Application(client_max_size=1 * 1024 * 1024)
    app["queue"] = asyncio.Queue()
    app.router.add_post("/v1/videos/generations", create_generation)
    app.router.add_get("/v1/videos/generations/{id}", get_generation)
    app.router.add_get("/v1/videos/files/{name}", serve_file)
    app.router.add_get("/v1/videos/models", list_models)
    # do-ai-compatible async video API (driven by the cloud LLM gateway)
    app.router.add_post("/v1/videos", doai_create)
    app.router.add_get("/v1/videos/{id}", doai_status)
    app.router.add_get("/v1/videos/{id}/content", doai_content)
    app.router.add_get("/healthz", healthz)

    async def _on_start(app):
        # requeue jobs interrupted by a restart
        with db() as c:
            for row in c.execute("SELECT id FROM jobs WHERE status IN ('queued','running')"):
                app["queue"].put_nowait(row["id"])
        app["worker"] = asyncio.create_task(worker(app))

    app.on_startup.append(_on_start)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
