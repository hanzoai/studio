"""Single client layer for the Hanzo Engine — the native-Rust, all-modality
inference backend served over its OpenAI-compatible HTTP API.

Transport, config, and Studio<->engine type conversions live here so the nodes
in `nodes.py` stay thin: one node == one endpoint. New engine endpoints
(music, dub, ...) become a new small node over this same layer, no framework
change.

Base URL resolves from the HANZO_ENGINE_URL env var (default
http://127.0.0.1:1234); a trailing `/v1` is tolerated and stripped so callers
always build `/v1/...` paths themselves. Engine-down and protocol errors raise
EngineError, which Studio reports on the failing node instead of crashing the
server.
"""
import base64
import io
import json
import os
import time

import numpy as np
import requests
import torch
from PIL import Image

DEFAULT_BASE_URL = "http://127.0.0.1:1234"
POLL_INTERVAL_S = 2.0


class EngineError(RuntimeError):
    """Raised on any engine transport or protocol failure."""


def base_url() -> str:
    raw = os.environ.get("HANZO_ENGINE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw or DEFAULT_BASE_URL


def _url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{base_url()}{path}"


def _down_msg(url: str, exc: Exception) -> str:
    return (
        f"Hanzo Engine request to {url} failed: {exc}. Is the engine running? "
        f"Start it with `hanzo engine serve` and set HANZO_ENGINE_URL "
        f"(default {DEFAULT_BASE_URL})."
    )


def _json_or_raise(resp: requests.Response, url: str) -> dict:
    if not resp.ok:
        raise EngineError(f"Hanzo Engine {url} returned HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise EngineError(f"Hanzo Engine {url} returned a non-JSON response: {resp.text[:200]}") from exc


# --- HTTP -------------------------------------------------------------------

def post_json(path: str, payload: dict, timeout: int = 120) -> dict:
    url = _url(path)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(_down_msg(url, exc)) from exc
    return _json_or_raise(resp, url)


def get_json(path: str, timeout: int = 30) -> dict:
    url = _url(path)
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(_down_msg(url, exc)) from exc
    return _json_or_raise(resp, url)


def post_bytes(path: str, payload: dict, timeout: int = 300) -> bytes:
    """POST JSON, return the raw response body (endpoints that return binary,
    e.g. /v1/audio/speech)."""
    url = _url(path)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(_down_msg(url, exc)) from exc
    if not resp.ok:
        raise EngineError(f"Hanzo Engine {url} returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.content


def post_multipart(path: str, files: dict, data: dict, timeout: int = 300) -> dict:
    url = _url(path)
    try:
        resp = requests.post(url, files=files, data=data, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(_down_msg(url, exc)) from exc
    return _json_or_raise(resp, url)


def get_bytes(path_or_url: str, timeout: int = 120) -> bytes:
    url = path_or_url if path_or_url.startswith("http") else _url(path_or_url)
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(f"Hanzo Engine fetch {url} failed: {exc}.") from exc
    if not resp.ok:
        raise EngineError(f"Hanzo Engine fetch {url} returned HTTP {resp.status_code}.")
    return resp.content


def list_models() -> list:
    """GET /v1/models -> [id, ...]. Returns [] if the engine is unreachable so
    node definitions never fail to load."""
    try:
        data = get_json("/v1/models", timeout=5)
    except EngineError:
        return []
    items = data.get("data") or []
    return [it["id"] for it in items if isinstance(it, dict) and it.get("id")]


def stream_chat(payload: dict, timeout: int = 300):
    """Yield content deltas from a streaming /v1/chat/completions (OpenAI SSE)."""
    url = _url("/v1/chat/completions")
    body = {**payload, "stream": True}
    try:
        resp = requests.post(url, json=body, stream=True, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise EngineError(_down_msg(url, exc)) from exc
    if not resp.ok:
        raise EngineError(f"Hanzo Engine {url} returned HTTP {resp.status_code}: {resp.text[:500]}")
    with resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if delta:
                yield delta


def extract_message_text(data: dict) -> str:
    try:
        content = (data["choices"][0].get("message") or {}).get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise EngineError(f"Unexpected Hanzo Engine response shape: {json.dumps(data)[:300]}") from exc
    if isinstance(content, list):  # OpenAI structured content parts
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text").strip()
    return (content or "").strip()


def run_job(resource: str, payload: dict, job_timeout: int, progress=None) -> bytes:
    """Submit an async generation job, poll to completion, return its content.

      POST /v1/{resource}              -> {"id": ...}
      GET  /v1/{resource}/{id}         -> {"status": ..., "progress"?: ...}
      GET  /v1/{resource}/{id}/content -> raw bytes
    """
    submit = post_json(f"/v1/{resource}", payload, timeout=60)
    job_id = submit.get("id")
    if not job_id:
        raise EngineError(f"Hanzo Engine /v1/{resource} did not return a job id: {submit}")
    deadline = time.monotonic() + job_timeout
    while True:
        body = get_json(f"/v1/{resource}/{job_id}", timeout=60)
        status = str(body.get("status", "")).lower()
        if status == "completed":
            break
        if status in {"failed", "cancelled", "error"}:
            msg = body.get("error") or body.get("message") or status
            raise EngineError(f"Hanzo Engine {resource} job {job_id} {status}: {msg}")
        if progress is not None:
            progress(body.get("progress"))
        if time.monotonic() > deadline:
            raise EngineError(
                f"Hanzo Engine {resource} job {job_id} did not complete within "
                f"{job_timeout}s (last status: {status or 'unknown'})."
            )
        time.sleep(POLL_INTERVAL_S)
    return get_bytes(f"/v1/{resource}/{job_id}/content", timeout=job_timeout)


def progress_bar(total: int):
    try:
        from comfy.utils import ProgressBar

        return ProgressBar(total)
    except Exception:
        return None


# --- Image conversions ------------------------------------------------------

def tensor_to_pil(image: torch.Tensor) -> Image.Image:
    if image.dim() == 4:  # [B,H,W,C] -> first frame
        image = image[0]
    arr = (image.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def pil_to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(img.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]  # [1,H,W,C]


def pil_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def tensor_to_data_url(image: torch.Tensor) -> str:
    return pil_to_data_url(tensor_to_pil(image))


def _decode_image_item(item: dict) -> Image.Image:
    b64 = item.get("b64_json")
    if b64:
        raw = base64.b64decode(b64)
    else:
        url = item.get("url")
        if not url:
            raise EngineError("Hanzo Engine image item has neither 'b64_json' nor 'url'.")
        raw = base64.b64decode(url.split(",", 1)[1]) if url.startswith("data:") else get_bytes(url)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def images_to_batch(items: list) -> torch.Tensor:
    if not items:
        raise EngineError("Hanzo Engine returned no image data.")
    tensors = [pil_to_tensor(_decode_image_item(it)) for it in items]
    if len({t.shape for t in tensors}) == 1:
        return torch.cat(tensors, dim=0)
    return tensors[0]


def mp4_bytes_to_image_batch(data: bytes) -> torch.Tensor:
    import av

    frames = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(video=0):
            frames.append(frame.to_ndarray(format="rgb24"))
    if not frames:
        raise EngineError("Hanzo Engine video job returned a video with no frames.")
    arr = np.stack(frames).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


# --- Audio conversions (Studio AUDIO == {waveform:[B,C,T], sample_rate}) -----

def _f32_pcm(wav: torch.Tensor) -> torch.Tensor:
    if wav.dtype.is_floating_point:
        return wav
    if wav.dtype == torch.int16:
        return wav.float() / (2 ** 15)
    if wav.dtype == torch.int32:
        return wav.float() / (2 ** 31)
    return wav.float()


def audio_bytes_to_studio(data: bytes) -> dict:
    """Decode engine speech bytes (any container) to a Studio AUDIO dict."""
    import av

    chunks, sr, n_ch = [], 24000, 1
    with av.open(io.BytesIO(data)) as af:
        if not af.streams.audio:
            raise EngineError("Hanzo Engine speech response has no audio stream.")
        stream = af.streams.audio[0]
        sr = stream.codec_context.sample_rate or sr
        n_ch = stream.channels or 1
        for frame in af.decode(streams=stream.index):
            buf = torch.from_numpy(frame.to_ndarray())
            if buf.shape[0] != n_ch:
                buf = buf.view(-1, n_ch).t()
            chunks.append(buf)
    wav = torch.cat(chunks, dim=1) if chunks else torch.zeros(n_ch, 0)
    return {"waveform": _f32_pcm(wav).unsqueeze(0), "sample_rate": int(sr)}


def studio_audio_to_wav_bytes(audio: dict) -> bytes:
    """Encode a Studio AUDIO dict ({waveform:[B,C,T], sample_rate}) to 16-bit PCM
    WAV bytes for multipart upload (ASR) or a data URL (dub). Stdlib `wave` only."""
    import wave

    wav = audio["waveform"]
    if wav.dim() == 3:  # [B,C,T] -> [C,T]
        wav = wav[0]
    arr = wav.detach().cpu().float().clamp_(-1.0, 1.0).numpy()  # [C,T]
    pcm16 = (arr * 32767.0).astype(np.int16)
    interleaved = pcm16.T.reshape(-1).tobytes()  # [T,C] interleaved
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(arr.shape[0])
        w.setsampwidth(2)
        w.setframerate(int(audio["sample_rate"]))
        w.writeframes(interleaved)
    return buf.getvalue()


def studio_audio_to_data_url(audio: dict) -> str:
    """Encode a Studio AUDIO dict to a `data:audio/wav` URL (driving audio for
    /v1/animate, which takes audio as an http/file/data URL)."""
    return "data:audio/wav;base64," + base64.b64encode(studio_audio_to_wav_bytes(audio)).decode("ascii")
