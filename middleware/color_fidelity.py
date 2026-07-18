"""Colour fidelity for Fix outputs.

A Qwen-Image-Edit fix runs at denoise 1.0 — it re-paints EVERY pixel, so the
render's global white-balance / exposure / saturation drift off the source, and
that drift COMPOUNDS across fix-of-a-fix. This restores the render's global colour
statistics to its source (Reinhard mean/std transfer, per RGB channel): the local
edit stays, the global cast goes. Matching an already-correct image to its source
is the identity, so a render that did not drift is left untouched.

Applied POD-SIDE when a BYO-GPU worker's finished render is ingested
(server.upload_output). It needs NO node on the GPU worker and touches no render
graph, so it can never make a render fail. Fail-safe throughout: any error, missing
source, or size mismatch leaves the raw bytes exactly as uploaded.
"""
import os

import numpy as np
from PIL import Image

# Fraction of the correction to apply. <1.0 leaves headroom for an INTENTIONAL
# global colour change in the instruction: the drift being corrected is a global
# cast, while a normal edit is local and barely moves the whole-frame statistics,
# so a moderate-high default corrects drift while an intended recolour mostly
# survives. Tunable without a rebuild via STUDIO_COLORMATCH_STRENGTH.
STRENGTH = float(os.environ.get("STUDIO_COLORMATCH_STRENGTH", "0.85"))


def color_match(target: Image.Image, reference: Image.Image, strength: float = STRENGTH) -> Image.Image:
    """Pull ``target``'s global colour balance onto ``reference``'s — a per-channel
    Reinhard mean/std transfer in RGB. target-to-itself is the identity."""
    t = np.asarray(target.convert("RGB"), np.float32)
    ref = reference.convert("RGB")
    if ref.size != target.size:
        ref = ref.resize(target.size, Image.LANCZOS)
    r = np.asarray(ref, np.float32)
    out = t.copy()
    for c in range(3):
        tm, ts = t[..., c].mean(), t[..., c].std() + 1e-5
        rm, rs = r[..., c].mean(), r[..., c].std() + 1e-5
        out[..., c] = (t[..., c] - tm) * (rs / ts) + rm
    out = t + (out - t) * float(strength)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def match_in_place(out_path: str, src_path: str, strength: float = STRENGTH) -> bool:
    """Colour-match the fix render at ``out_path`` to its source, atomically in
    place (tmp + replace). Returns True if rewritten, False (no-op) on any error or
    missing input — never raises."""
    try:
        if strength <= 0 or not (os.path.isfile(out_path) and os.path.isfile(src_path)):
            return False
        with Image.open(out_path) as o, Image.open(src_path) as s:
            matched = color_match(o, s, strength)
        # Tmp keeps a real image extension so PIL can infer the format; pass it
        # explicitly too (the ".cm.tmp" suffix alone is unknown to PIL).
        ext = os.path.splitext(out_path)[1].lower()
        fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(ext, "PNG")
        tmp = f"{out_path}.cm.tmp{ext or '.png'}"
        matched.save(tmp, format=fmt)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False
