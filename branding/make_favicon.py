#!/usr/bin/env python3
"""Render branding/favicon.svg to a multi-resolution favicon.ico.

Source of truth is favicon.svg (the Hanzo mark). The .ico is a committed build
artifact so that apply-branding.sh can install it without needing a rasterizer
in the Docker image. Re-run this whenever favicon.svg changes:

    rsvg-convert must be on PATH (librsvg2-bin); Pillow provides ICO packing.
    python3 branding/make_favicon.py
"""
import os
import subprocess
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "favicon.svg")
ICO = os.path.join(HERE, "favicon.ico")
SIZES = [16, 32, 48, 64, 128, 256]


def render_png(size: int, path: str) -> None:
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), SVG, "-o", path],
        check=True,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for size in SIZES:
            png = os.path.join(tmp, f"favicon-{size}.png")
            render_png(size, png)
            frames.append(Image.open(png).convert("RGBA"))
        base = frames[-1]
        base.save(ICO, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {ICO} ({os.path.getsize(ICO)} bytes) sizes={SIZES}")


if __name__ == "__main__":
    main()
