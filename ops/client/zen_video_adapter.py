#!/usr/bin/env python3
"""
zen3-video adapter: OpenAI-style async video-generation API in front of the
local Hanzo Studio (ComfyUI) instance on the GB10.

  POST /v1/video/generations        -> enqueue, returns {id, status:"queued"}
  GET  /v1/video/generations/{id}   -> poll, returns status + playable url when done
  GET  /v1/models                   -> lists zen3-video
  GET  /healthz                     -> liveness + upstream ComfyUI reachability

Design:
  * ComfyUI's native /prompt is the job QUEUE (one GPU job at a time; extra
    requests queue). The prompt_id IS the job id -- no second queue needed.
  * The Wan2.2-TI2V-5B graph is built here from verified node schemas. The
    upstream model is never named in any response: the public model is
    "zen3-video" (brand policy).
  * Output is served back through whatever PUBLIC_BASE points at (the spark
    Cloudflare tunnel), so the returned url is directly playable. If S3_* env
    is set, the mp4 is also mirrored to S3 and that url is returned instead.

Deps: aiohttp only (already in the Studio venv). No new packages.
"""
import os
import time
import uuid
import logging
from aiohttp import web, ClientSession, ClientTimeout

log = logging.getLogger("zen3-video")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
# Public base the generated file is reachable at (spark cloudflare tunnel).
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", COMFY).rstrip("/")
LISTEN_PORT = int(os.environ.get("ZEN_VIDEO_PORT", "8189"))
MODEL_NAME = "zen3-video"

# ---- upstream model files (native ComfyUI single-file format) --------------
WAN_UNET = os.environ.get("ZEN_VIDEO_UNET", "wan2.2_ti2v_5B_fp16.safetensors")
WAN_CLIP = os.environ.get("ZEN_VIDEO_CLIP", "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
WAN_VAE = os.environ.get("ZEN_VIDEO_VAE", "wan2.2_vae.safetensors")

DEFAULT_NEG = ("blurry, low quality, distorted, watermark, static image, "
               "jpeg artifacts, deformed, extra limbs")

CLIENT_ID = "zen3-video-adapter"


def _round32(v: int) -> int:
    v = max(32, int(v))
    return v - (v % 32)


def _round4_len(v: int) -> int:
    # Wan latent temporal stride is 4: length must satisfy (length-1) % 4 == 0
    v = max(1, int(v))
    return ((v - 1) // 4) * 4 + 1


def build_graph(params: dict) -> dict:
    """Build the Wan2.2-TI2V-5B ComfyUI API graph (t2v, or i2v if start_image)."""
    width = _round32(params.get("width", 640))
    height = _round32(params.get("height", 384))
    length = _round4_len(params.get("num_frames", 25))
    steps = int(params.get("steps", 20))
    cfg = float(params.get("cfg", 5.0))
    fps = float(params.get("fps", 16.0))
    shift = float(params.get("shift", 8.0))
    seed = int(params.get("seed", uuid.uuid4().int % (2**31)))
    prompt = params["prompt"]
    negative = params.get("negative_prompt") or DEFAULT_NEG
    start_image = params.get("_start_image_name")  # uploaded ComfyUI input filename

    g = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_CLIP, "type": "wan"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": WAN_VAE}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "ModelSamplingSD3",
              "inputs": {"model": ["1", 0], "shift": shift}},
        "7": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": width, "height": height,
                         "length": length, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["6", 0], "seed": seed, "steps": steps,
                         "cfg": cfg, "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["7", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo",
               "inputs": {"images": ["9", 0], "fps": fps}},
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0], "filename_prefix": "zen3-video/clip",
                          "format": "auto", "codec": "auto"}},
    }
    if start_image:
        # image-to-video: feed the uploaded frame as the conditioning start image
        g["12"] = {"class_type": "LoadImage", "inputs": {"image": start_image}}
        g["7"]["inputs"]["start_image"] = ["12", 0]
    return g


async def comfy_post(session, path, payload):
    async with session.post(f"{COMFY}{path}", json=payload) as r:
        r.raise_for_status()
        return await r.json()


async def comfy_get(session, path):
    async with session.get(f"{COMFY}{path}") as r:
        r.raise_for_status()
        return await r.json()


async def handle_create(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json body"}, status=400)

    model = body.get("model", MODEL_NAME)
    if model not in (MODEL_NAME, "zen3-video-i2v"):
        return web.json_response(
            {"error": f"unknown model '{model}', use '{MODEL_NAME}'"}, status=400)
    if not body.get("prompt"):
        return web.json_response({"error": "'prompt' is required"}, status=400)

    # size "WxH" convenience
    if "size" in body and "x" in str(body["size"]):
        w, h = str(body["size"]).lower().split("x")
        body["width"], body["height"] = int(w), int(h)
    # seconds -> frames convenience
    if "seconds" in body and "num_frames" not in body:
        body["num_frames"] = int(float(body["seconds"]) * float(body.get("fps", 16)))

    timeout = ClientTimeout(total=60)
    async with ClientSession(timeout=timeout) as session:
        # optional i2v: accept an image url and stage it into ComfyUI input dir
        if body.get("image"):
            try:
                name = await stage_input_image(session, body["image"])
                body["_start_image_name"] = name
            except Exception as e:
                return web.json_response({"error": f"image fetch failed: {e}"}, status=400)
        graph = build_graph(body)
        try:
            res = await comfy_post(session, "/prompt",
                                   {"prompt": graph, "client_id": CLIENT_ID})
        except Exception as e:
            log.exception("enqueue failed")
            return web.json_response({"error": f"enqueue failed: {e}"}, status=502)

    pid = res.get("prompt_id")
    if not pid:
        return web.json_response({"error": "no prompt_id from upstream",
                                  "detail": res}, status=502)
    return web.json_response({
        "id": pid,
        "object": "video.generation",
        "model": MODEL_NAME,
        "status": "queued",
        "queue_number": res.get("number"),
        "created": int(time.time()),
    })


async def stage_input_image(session, url_or_data: str) -> str:
    """Download an image url and POST it to ComfyUI /upload/image; return its name."""
    import base64
    if url_or_data.startswith("data:"):
        raw = base64.b64decode(url_or_data.split(",", 1)[1])
    else:
        async with session.get(url_or_data) as r:
            r.raise_for_status()
            raw = await r.read()
    name = f"zen3_i2v_{uuid.uuid4().hex[:12]}.png"
    form = web.FormData()
    form.add_field("image", raw, filename=name, content_type="image/png")
    async with session.post(f"{COMFY}/upload/image", data=form) as r:
        r.raise_for_status()
        j = await r.json()
    return j.get("name", name)


def _extract_video(history_entry: dict):
    outs = history_entry.get("outputs", {})
    for node_out in outs.values():
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key, []) or []:
                fn = item.get("filename")
                if fn and fn.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                    return item
    return None


async def handle_status(request: web.Request):
    pid = request.match_info["id"]
    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        try:
            hist = await comfy_get(session, f"/history/{pid}")
        except Exception as e:
            return web.json_response({"error": f"upstream error: {e}"}, status=502)

    if pid not in hist:
        # still queued or running -- check queue for position
        async with ClientSession(timeout=timeout) as session:
            try:
                q = await comfy_get(session, "/queue")
            except Exception:
                q = {}
        running = any(pid == str(x[1]) for x in q.get("queue_running", []))
        return web.json_response({
            "id": pid, "object": "video.generation", "model": MODEL_NAME,
            "status": "processing" if running else "queued",
        })

    entry = hist[pid]
    status = entry.get("status", {})
    if status.get("status_str") == "error" or status.get("completed") is False:
        return web.json_response({
            "id": pid, "model": MODEL_NAME, "status": "failed",
            "error": status,
        })
    vid = _extract_video(entry)
    if not vid:
        return web.json_response({
            "id": pid, "model": MODEL_NAME, "status": "processing",
        })

    from urllib.parse import urlencode
    qs = urlencode({"filename": vid["filename"],
                    "subfolder": vid.get("subfolder", ""),
                    "type": vid.get("type", "output")})
    url = f"{PUBLIC_BASE}/view?{qs}"
    resp = {
        "id": pid, "object": "video.generation", "model": MODEL_NAME,
        "status": "succeeded",
        "url": url,
        "filename": vid["filename"],
    }
    # optional S3 mirror
    s3_url = await maybe_mirror_s3(vid)
    if s3_url:
        resp["url"] = s3_url
        resp["source_url"] = url
    return web.json_response(resp)


async def maybe_mirror_s3(vid: dict):
    """If S3_ENDPOINT/S3_BUCKET/S3 creds are set, mirror the mp4 and return its url."""
    endpoint = os.environ.get("S3_ENDPOINT")
    bucket = os.environ.get("S3_BUCKET")
    if not (endpoint and bucket):
        return None
    try:
        import boto3  # optional; only used if S3 configured
        from urllib.parse import urlencode
        timeout = ClientTimeout(total=120)
        async with ClientSession(timeout=timeout) as session:
            qs = urlencode({"filename": vid["filename"],
                            "subfolder": vid.get("subfolder", ""),
                            "type": vid.get("type", "output")})
            async with session.get(f"{COMFY}/view?{qs}") as r:
                r.raise_for_status()
                data = await r.read()
        s3 = boto3.client(
            "s3", endpoint_url=endpoint,
            aws_access_key_id=os.environ["S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name=os.environ.get("S3_REGION", "us-east-1"))
        key = f"zen3-video/{os.path.basename(vid['filename'])}"
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="video/mp4")
        pub = os.environ.get("S3_PUBLIC_BASE", endpoint.rstrip("/") + "/" + bucket)
        return f"{pub.rstrip('/')}/{key}"
    except Exception as e:
        log.warning("s3 mirror skipped: %s", e)
        return None


async def handle_models(request):
    return web.json_response({"object": "list", "data": [
        {"id": MODEL_NAME, "object": "model", "owned_by": "zenlm",
         "capabilities": ["text-to-video", "image-to-video"]}]})


async def handle_health(request):
    ok = False
    detail = "unreachable"
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as s:
            async with s.get(f"{COMFY}/system_stats") as r:
                ok = r.status == 200
                detail = (await r.json()).get("devices", [{}])[0].get("name")
    except Exception as e:
        detail = str(e)
    return web.json_response({"status": "ok" if ok else "degraded",
                              "comfy": COMFY, "device": detail})


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/v1/video/generations", handle_create)
    app.router.add_get("/v1/video/generations/{id}", handle_status)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/healthz", handle_health)
    return app


if __name__ == "__main__":
    log.info("zen3-video adapter -> %s  (public=%s)  port=%d",
             COMFY, PUBLIC_BASE, LISTEN_PORT)
    web.run_app(make_app(), host="0.0.0.0", port=LISTEN_PORT, access_log=log)
