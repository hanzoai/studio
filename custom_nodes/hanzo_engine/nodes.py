"""Hanzo Engine nodes for Hanzo Studio.

These nodes call a local Hanzo Engine — the native-Rust, all-modality inference
and training backend — over its OpenAI-compatible HTTP API. The base URL comes
from the HANZO_ENGINE_URL environment variable (default http://127.0.0.1:1234/v1).

Connection and protocol errors surface as clear node errors (raised
RuntimeError), which Hanzo Studio reports on the failing node rather than
crashing the server.
"""
import base64
import io
import json
import os

import numpy as np
import requests
import torch
from PIL import Image

import folder_paths

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"


def _base_url() -> str:
    return os.environ.get("HANZO_ENGINE_URL", DEFAULT_BASE_URL).rstrip("/")


def _post(path: str, payload: dict, timeout: int = 120) -> dict:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Hanzo Engine request to {url} failed: {exc}. Is the engine running? "
            f"Set HANZO_ENGINE_URL (default {DEFAULT_BASE_URL})."
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Hanzo Engine {url} returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Hanzo Engine {url} returned a non-JSON response: {resp.text[:200]}"
        ) from exc


def _get_bytes(url: str, timeout: int = 120) -> bytes:
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Hanzo Engine image fetch from {url} failed: {exc}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Hanzo Engine image fetch {url} returned HTTP {resp.status_code}.")
    return resp.content


def _extract_message_text(data: dict) -> str:
    try:
        content = (data["choices"][0].get("message") or {}).get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Unexpected Hanzo Engine response shape: {json.dumps(data)[:300]}"
        ) from exc
    if isinstance(content, list):  # OpenAI structured content parts
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(parts).strip()
    return (content or "").strip()


def _tensor_to_pil(image: torch.Tensor) -> Image.Image:
    if image.dim() == 4:  # [B,H,W,C] -> first frame
        image = image[0]
    arr = (image.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def _pil_to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(img.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]  # [1,H,W,C]


def _pil_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _decode_image_item(item: dict) -> Image.Image:
    b64 = item.get("b64_json")
    if b64:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    url = item.get("url")
    if url:
        if url.startswith("data:"):
            raw = base64.b64decode(url.split(",", 1)[1])
        else:
            raw = _get_bytes(url)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    raise RuntimeError("Hanzo Engine image item has neither 'b64_json' nor 'url'.")


class HanzoChat:
    """Text chat via Hanzo Engine /v1/chat/completions. Use for prompt
    engineering, caption cleanup, and text chains inside a graph."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "default"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "system": ("STRING", {"multiline": True, "default": ""}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 512, "min": 1, "max": 32768}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "generate"
    CATEGORY = "Hanzo/Engine"

    def generate(self, model, prompt, system="", temperature=0.7, max_tokens=512):
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        data = _post(
            "/chat/completions",
            {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        )
        return (_extract_message_text(data),)


class HanzoImageGen:
    """Text-to-image via Hanzo Engine /v1/images/generations. Returns a
    Hanzo Studio IMAGE tensor."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "size": (["1024x1024", "1024x1536", "1536x1024", "768x768", "512x512"], {"default": "1024x1024"}),
            },
            "optional": {
                "model": ("STRING", {"default": "default"}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 150}),
                "n": ("INT", {"default": 1, "min": 1, "max": 8}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"
    CATEGORY = "Hanzo/Engine"

    def generate(self, prompt, size, model="default", steps=20, n=1):
        data = _post(
            "/images/generations",
            {"model": model, "prompt": prompt, "size": size, "n": n, "steps": steps, "response_format": "b64_json"},
            timeout=600,
        )
        items = data.get("data") or []
        if not items:
            raise RuntimeError("Hanzo Engine /images/generations returned no image data.")
        tensors = [_pil_to_tensor(_decode_image_item(it)) for it in items]
        if len(tensors) == 1:
            batch = tensors[0]
        else:
            same = len({t.shape for t in tensors}) == 1
            batch = torch.cat(tensors, dim=0) if same else tensors[0]
        return (batch,)


class HanzoVisionCaption:
    """Caption an IMAGE via Hanzo Engine vision chat (OpenAI image_url format).
    Feeds LoRA/dataset prep loops; pair with a vision model such as Qwen3-VL."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "prompt": ("STRING", {"multiline": True, "default": "Describe this image in detail for a training caption."}),
            },
            "optional": {
                "model": ("STRING", {"default": "default"}),
                "system": ("STRING", {"multiline": True, "default": ""}),
                "max_tokens": ("INT", {"default": 512, "min": 1, "max": 32768}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("caption",)
    FUNCTION = "caption"
    CATEGORY = "Hanzo/Engine"

    def caption(self, image, prompt, model="default", system="", max_tokens=512):
        data_url = _pil_to_data_url(_tensor_to_pil(image))
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        })
        data = _post("/chat/completions", {"model": model, "messages": messages, "max_tokens": max_tokens}, timeout=300)
        return (_extract_message_text(data),)


class HanzoSaveText:
    """Write a STRING to a .txt file in the output directory (image/caption
    pairs for dataset prep) and preview it in the node."""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "filename_prefix": ("STRING", {"default": "caption"}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Hanzo/Engine"

    def save(self, text, filename_prefix="caption"):
        full_output_folder, filename, counter, _subfolder, _prefix = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir
        )
        path = os.path.join(full_output_folder, f"{filename}_{counter:05}_.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"ui": {"text": [text]}}


NODE_CLASS_MAPPINGS = {
    "HanzoChat": HanzoChat,
    "HanzoImageGen": HanzoImageGen,
    "HanzoVisionCaption": HanzoVisionCaption,
    "HanzoSaveText": HanzoSaveText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "HanzoChat": "Hanzo Chat (Engine)",
    "HanzoImageGen": "Hanzo Image Gen (Engine)",
    "HanzoVisionCaption": "Hanzo Vision Caption (Engine)",
    "HanzoSaveText": "Hanzo Save Text",
}
