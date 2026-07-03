"""Zen video: Wan2.2 TI2V-5B text-to-video ComfyUI API-format workflow builder.

Public model id: zen3-video. Private backend: wan2.2_ti2v_5B_fp16.
Never expose the upstream name in any public surface.
"""
from __future__ import annotations
import random

# Wan2.2 default negative prompt (matches the official ComfyUI TI2V template).
DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，"
    "字幕，风格，作品，画作，画面，静止，"
    "整体发灰，最差质量，低质量，JPEG压缩残留，"
    "丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
    "手指融合，静止不动的画面，杂乱的背景，三条腿，"
    "背景人很多，倒着走"
)

# Model filenames as present on spark (models/ dir).
UNET = "wan2.2_ti2v_5B_fp16.safetensors"
CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
VAE = "wan2.2_vae.safetensors"


def build_prompt(
    *,
    positive: str,
    negative: str | None = None,
    width: int = 1280,
    height: int = 704,
    length: int = 121,
    steps: int = 20,
    cfg: float = 5.0,
    shift: float = 8.0,
    sampler: str = "uni_pc",
    scheduler: str = "simple",
    fps: int = 24,
    seed: int | None = None,
    filename_prefix: str = "video/zen",
) -> dict:
    """Return a ComfyUI /prompt API-format graph for Wan2.2 TI2V-5B text-to-video.

    length must be 4n+1 (temporal VAE stride 4). width/height multiples of 16.
    """
    if negative is None:
        negative = DEFAULT_NEGATIVE
    if seed is None:
        seed = random.randint(0, 2**53 - 1)
    # snap length to 4n+1
    if (length - 1) % 4 != 0:
        length = ((length - 1) // 4) * 4 + 1
    width -= width % 16
    height -= height % 16

    return {
        "37": {"class_type": "UNETLoader",
               "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "38": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": CLIP, "type": "wan", "device": "default"}},
        "39": {"class_type": "VAELoader",
               "inputs": {"vae_name": VAE}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["38", 0]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["38", 0]}},
        "48": {"class_type": "ModelSamplingSD3",
               "inputs": {"model": ["37", 0], "shift": shift}},
        "55": {"class_type": "Wan22ImageToVideoLatent",
               "inputs": {"vae": ["39", 0], "width": width, "height": height,
                          "length": length, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["48", 0], "seed": seed, "steps": steps,
                         "cfg": cfg, "sampler_name": sampler, "scheduler": scheduler,
                         "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["55", 0], "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "57": {"class_type": "CreateVideo",
               "inputs": {"images": ["8", 0], "fps": fps}},
        "58": {"class_type": "SaveVideo",
               "inputs": {"video": ["57", 0], "filename_prefix": filename_prefix,
                          "format": "mp4", "codec": "h264"}},
    }, seed


if __name__ == "__main__":
    import json, sys
    p, s = build_prompt(positive=sys.argv[1] if len(sys.argv) > 1 else "a cat",
                        width=640, height=640, length=41, steps=20)
    print(json.dumps({"prompt": p}, ensure_ascii=False))
