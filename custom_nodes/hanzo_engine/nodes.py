"""Hanzo Engine nodes for Hanzo Studio.

Each node is one endpoint of the native-Rust Hanzo Engine, over the shared
client in `client.py`. Config is one way: the engine base URL (default
http://127.0.0.1:1234, override with HANZO_ENGINE_URL). Paths are always
`/v1/*` — never `/api/*`.

  HanzoChat          -> POST /v1/chat/completions           (prompt -> text)
  HanzoImageGen      -> POST /v1/images/generations         (prompt -> IMAGE)
  HanzoTTS           -> POST /v1/audio/speech               (text -> AUDIO)
  HanzoASR           -> POST /v1/audio/transcriptions       (AUDIO -> text)
  HanzoEngineRequest -> POST|GET <any /v1 path>             (JSON -> response)
  HanzoVisionCaption -> POST /v1/chat/completions (vision)  (IMAGE -> text)
  HanzoTextToVideo   -> async /v1/videos                    (prompt -> frames)
  HanzoImageTo3D     -> async /v1/3d                        (IMAGE -> mesh path)
  HanzoSaveText      -> writes a STRING to output/*.txt

The generic HanzoEngineRequest is the forward-compatible escape hatch: a new
engine endpoint (music, dub, ...) is reachable the day it ships, and promoting
it to a first-class node is a few lines here.
"""
import json
import os

import folder_paths

from . import client

CATEGORY = "Hanzo/Engine"


def _model_combo() -> list:
    """["default", <ids from /v1/models>] — 'default' always routes to the
    loaded model, so saved graphs stay portable across engines."""
    combo, seen = [], set()
    for name in ["default", *client.list_models()]:
        if name not in seen:
            seen.add(name)
            combo.append(name)
    return combo


class HanzoChat:
    """LLM chat/completion. Streams tokens (live progress) and returns the
    full text; feeds prompt chains and image prompts inside a graph."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (_model_combo(),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "system": ("STRING", {"multiline": True, "default": ""}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 512, "min": 1, "max": 32768}),
                "stream": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def generate(self, model, prompt, system="", temperature=0.7, max_tokens=512, stream=True):
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        if stream:
            pbar = client.progress_bar(max_tokens)
            parts = []
            for i, delta in enumerate(client.stream_chat(payload)):
                parts.append(delta)
                if pbar:
                    pbar.update_absolute(min(i + 1, max_tokens))
            text = "".join(parts).strip()
        else:
            text = client.extract_message_text(client.post_json("/v1/chat/completions", payload))
        return {"ui": {"text": [text]}, "result": (text,)}


class HanzoImageGen:
    """Text-to-image via /v1/images/generations (native Rust). Returns a
    Studio IMAGE tensor."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "width": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
                "height": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            },
            "optional": {
                "model": (_model_combo(),),
                "n": ("INT", {"default": 1, "min": 1, "max": 8}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"
    CATEGORY = CATEGORY

    def generate(self, prompt, width, height, model="default", n=1):
        data = client.post_json(
            "/v1/images/generations",
            {"model": model, "prompt": prompt, "width": width, "height": height,
             "n": n, "response_format": "b64_json"},
            timeout=600,
        )
        return (client.images_to_batch(data.get("data") or []),)


class HanzoTTS:
    """Text-to-speech via /v1/audio/speech (native Rust). Returns a Studio
    AUDIO you can save/preview with the built-in audio nodes."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "model": (_model_combo(),),
                "response_format": (["wav", "flac", "mp3", "opus", "aac"], {"default": "wav"}),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "speak"
    CATEGORY = CATEGORY

    def speak(self, text, model="default", response_format="wav"):
        raw = client.post_bytes(
            "/v1/audio/speech",
            {"model": model, "input": text, "response_format": response_format},
        )
        return (client.audio_bytes_to_studio(raw),)


class HanzoASR:
    """Speech-to-text via /v1/audio/transcriptions (OpenAI multipart). Returns
    the transcript text."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
            },
            "optional": {
                "model": (_model_combo(),),
                "language": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "transcribe"
    CATEGORY = CATEGORY

    def transcribe(self, audio, model="default", language=""):
        wav = client.studio_audio_to_wav_bytes(audio)
        data = {"model": model, "response_format": "json"}
        if language.strip():
            data["language"] = language.strip()
        resp = client.post_multipart(
            "/v1/audio/transcriptions",
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
        )
        return ((resp.get("text") or "").strip(),)


class HanzoEngineRequest:
    """Generic engine call: any `/v1` path with a JSON body -> the raw JSON
    response as STRING. The forward-compatible escape hatch for endpoints that
    do not have a dedicated node yet (music, dub, ...)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "method": (["POST", "GET"], {"default": "POST"}),
                "path": ("STRING", {"default": "/v1/chat/completions"}),
                "json_body": ("STRING", {"multiline": True, "default": "{}"}),
            },
            "optional": {
                "timeout": ("INT", {"default": 120, "min": 1, "max": 3600}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("response",)
    FUNCTION = "call"
    CATEGORY = CATEGORY

    def call(self, method, path, json_body, timeout=120):
        try:
            payload = json.loads(json_body or "{}")
        except ValueError as exc:
            raise client.EngineError(f"HanzoEngineRequest json_body is not valid JSON: {exc}") from exc
        if method == "GET":
            data = client.get_json(path, timeout=timeout)
        else:
            data = client.post_json(path, payload, timeout=timeout)
        return (json.dumps(data, indent=2),)


class HanzoVisionCaption:
    """Caption an IMAGE via vision chat (OpenAI image_url). Pair with a vision
    model (e.g. Qwen3-VL) for LoRA/dataset prep."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "prompt": ("STRING", {"multiline": True,
                                      "default": "Describe this image in detail for a training caption."}),
            },
            "optional": {
                "model": (_model_combo(),),
                "system": ("STRING", {"multiline": True, "default": ""}),
                "max_tokens": ("INT", {"default": 512, "min": 1, "max": 32768}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("caption",)
    FUNCTION = "caption"
    CATEGORY = CATEGORY

    def caption(self, image, prompt, model="default", system="", max_tokens=512):
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": client.tensor_to_data_url(image)}},
        ]
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        data = client.post_json(
            "/v1/chat/completions",
            {"model": model, "messages": messages, "max_tokens": max_tokens},
            timeout=300,
        )
        return (client.extract_message_text(data),)


class HanzoTextToVideo:
    """Text-to-video via the async job /v1/videos (native Rust). Returns the
    decoded frames as an IMAGE batch so it composes with video-save nodes."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "num_frames": ("INT", {"default": 81, "min": 1, "max": 1024}),
                "width": ("INT", {"default": 832, "min": 64, "max": 2048, "step": 8}),
                "height": ("INT", {"default": 480, "min": 64, "max": 2048, "step": 8}),
                "steps": ("INT", {"default": 30, "min": 1, "max": 150}),
            },
            "optional": {
                "model": (_model_combo(),),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("frames",)
    FUNCTION = "generate"
    CATEGORY = CATEGORY

    def generate(self, prompt, num_frames, width, height, steps, model="default"):
        pbar = client.progress_bar(100)

        def report(p):
            if pbar is not None and p is not None:
                try:
                    pct = float(p)
                    pbar.update_absolute(max(0, min(100, int(pct * (100 if pct <= 1.0 else 1)))))
                except (TypeError, ValueError):
                    pass

        raw = client.run_job(
            "videos",
            {"model": model, "prompt": prompt, "num_frames": num_frames,
             "width": width, "height": height, "steps": steps},
            job_timeout=1800,
            progress=report,
        )
        return (client.mp4_bytes_to_image_batch(raw),)


class HanzoImageTo3D:
    """Image-to-3D via the async job /v1/3d (native Rust). Saves the GLB/PLY to
    the output dir and returns its path for a 3D-viewer node."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "format": (["glb", "ply"], {"default": "glb"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "steps": ("INT", {"default": 30, "min": 1, "max": 150}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("mesh_path",)
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def generate(self, image, format, seed, steps):
        raw = client.run_job(
            "3d",
            {"image": client.tensor_to_data_url(image), "format": format, "seed": seed, "steps": steps},
            job_timeout=900,
        )
        full_output_folder, filename, counter, _sub, _pre = folder_paths.get_save_image_path(
            "hanzo3d", folder_paths.get_output_directory()
        )
        path = os.path.join(full_output_folder, f"{filename}_{counter:05}_.{format}")
        with open(path, "wb") as fh:
            fh.write(raw)
        return {"ui": {"text": [path]}, "result": (path,)}


class HanzoSaveText:
    """Write a STRING to output/*.txt (image/caption pairs for dataset prep)
    and preview it in the node."""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "filename_prefix": ("STRING", {"default": "hanzo"}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY

    def save(self, text, filename_prefix="hanzo"):
        full_output_folder, filename, counter, _sub, _pre = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir
        )
        path = os.path.join(full_output_folder, f"{filename}_{counter:05}_.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"ui": {"text": [text]}}


NODE_CLASS_MAPPINGS = {
    "HanzoChat": HanzoChat,
    "HanzoImageGen": HanzoImageGen,
    "HanzoTTS": HanzoTTS,
    "HanzoASR": HanzoASR,
    "HanzoEngineRequest": HanzoEngineRequest,
    "HanzoVisionCaption": HanzoVisionCaption,
    "HanzoTextToVideo": HanzoTextToVideo,
    "HanzoImageTo3D": HanzoImageTo3D,
    "HanzoSaveText": HanzoSaveText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "HanzoChat": "Hanzo Chat (Engine)",
    "HanzoImageGen": "Hanzo Image Gen (Engine)",
    "HanzoTTS": "Hanzo TTS (Engine)",
    "HanzoASR": "Hanzo ASR (Engine)",
    "HanzoEngineRequest": "Hanzo Engine Request",
    "HanzoVisionCaption": "Hanzo Vision Caption (Engine)",
    "HanzoTextToVideo": "Hanzo Text to Video (Engine)",
    "HanzoImageTo3D": "Hanzo Image to 3D (Engine)",
    "HanzoSaveText": "Hanzo Save Text",
}
