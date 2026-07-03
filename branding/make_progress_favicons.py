#!/usr/bin/env python3
"""Render the 10 animated progress-favicon frames with the Hanzo mark.

During a generation the frontend cycles
`/assets/images/favicon_progress_16x16/frame_{0..9}.png` as the browser-tab
favicon. Upstream ships ComfyUI-branded frames, so the tab reverts to the Comfy
mark whenever a job runs. These frames are the Hanzo mark (white on a black tile,
matching favicon.svg) with a Hanzo-purple progress ring that fills 0 -> 100%.

Like favicon.ico, the PNGs are committed build artifacts so apply-branding.sh can
install them without a rasterizer in the Docker image. Re-run only if the mark or
palette changes:  python3 branding/make_progress_favicons.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "favicon_progress_16x16")

SS = 128            # supersampled canvas, downsampled to 16x16
FRAMES = 10
MARK_SCALE = 0.92
RING_BBOX = (12, 12, SS - 12, SS - 12)
RING_WIDTH = 10
BG = (0, 0, 0, 255)          # black tile, like favicon.svg
MARK = (255, 255, 255, 255)  # white Hanzo mark
RING = (139, 92, 246, 255)   # Hanzo purple #8B5CF6

# Hanzo mark: five subpaths from branding/hanzo-logo.svg, as polygons (0..67 space).
POLYS = [
    [(22.21, 67), (22.21, 44.6369), (0, 44.6369), (0, 67)],
    [(66.7038, 22.3184), (22.2534, 22.3184), (0.0878906, 44.6367), (44.4634, 44.6367)],
    [(22.21, 0), (0, 0), (0, 22.3184), (22.21, 22.3184)],
    [(66.7198, 0), (44.5098, 0), (44.5098, 22.3184), (66.7198, 22.3184)],
    [(66.7198, 67), (66.7198, 44.6369), (44.5098, 44.6369), (44.5098, 67)],
]
MARK_W = 66.7198
MARK_H = 67.0
OFF_X = (SS - MARK_W * MARK_SCALE) / 2
OFF_Y = (SS - MARK_H * MARK_SCALE) / 2


def draw_frame(frac: float) -> Image.Image:
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, SS - 1, SS - 1), radius=20, fill=BG)
    for poly in POLYS:
        pts = [(x * MARK_SCALE + OFF_X, y * MARK_SCALE + OFF_Y) for x, y in poly]
        d.polygon(pts, fill=MARK)
    if frac > 0:
        d.arc(RING_BBOX, start=-90, end=-90 + frac * 360, fill=RING, width=RING_WIDTH)
    return img.resize((16, 16), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for i in range(FRAMES):
        frac = (i + 1) / FRAMES
        draw_frame(frac).save(os.path.join(OUT, f"frame_{i}.png"))
    print(f"Wrote {FRAMES} progress frames to {OUT}")


if __name__ == "__main__":
    main()
