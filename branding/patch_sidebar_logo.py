#!/usr/bin/env python3
r"""Replace the in-app top-left sidebar logo (the ComfyUI mark) with the Hanzo mark.

The sidebar logo is NOT a file asset — it is an inline single-path SVG compiled
into the `ComfyLogo` Vue component inside a hashed JS chunk (e.g. GraphView-*.js).
`branding/apply-branding.sh` already swaps the file-based logos in assets/images/;
this handles the inline one that pass missed.

We key off the stable Vue component name `__name:\`ComfyLogo\`` so the patch replays
across frontend-package upgrades even as chunk hashes and minified identifiers change.
Within that component we rewrite the viewBox and the single path `d` to the Hanzo
mark (five filled rectangles forming the H glyph, in a 0..67 coordinate space).
The component renders with mode="fill" and color=currentColor(white), so the mark
fills white exactly like the original.
"""
import os
import re
import sys

STATIC_DIR = sys.argv[1]

HANZO_VIEWBOX = "0 0 67 67"
# Five subpaths in one `d`, matching branding/hanzo-logo.svg (0..67 space).
HANZO_PATH = (
    "M22.21 67V44.6369H0V67H22.21Z"
    "M66.7038 22.3184H22.2534L0.0878906 44.6367H44.4634L66.7038 22.3184Z"
    "M22.21 0H0V22.3184H22.21V0Z"
    "M66.7198 0H44.5098V22.3184H66.7198V0Z"
    "M66.7198 67V44.6369H44.5098V67H66.7198Z"
)

ANCHOR = "__name:`ComfyLogo`"
SEGMENT_LEN = 3000


def patch_content(content: str):
    idx = content.find(ANCHOR)
    if idx == -1:
        return content, False
    head, seg, tail = content[:idx], content[idx:idx + SEGMENT_LEN], content[idx + SEGMENT_LEN:]
    orig = seg
    seg = re.sub(r'viewBox:`0 0 [0-9.]+ [0-9.]+`', f'viewBox:`{HANZO_VIEWBOX}`', seg, count=1)
    seg = re.sub(r'd:`M[0-9][^`]*`', f'd:`{HANZO_PATH}`', seg, count=1)
    if seg == orig:
        return content, False
    return head + seg + tail, True


def main() -> int:
    found = 0
    for root, _dirs, files in os.walk(STATIC_DIR):
        for fname in files:
            if not fname.endswith(".js"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            if ANCHOR not in content:
                continue
            found += 1
            new_content, changed = patch_content(content)
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"  Replaced ComfyLogo sidebar mark in {fname}")
            elif HANZO_PATH in content:
                print(f"  ComfyLogo already Hanzo mark in {fname}")
            else:
                print(f"  WARNING: ComfyLogo in {fname} but path not matched")
                return 1
    if found == 0:
        print("  WARNING: ComfyLogo component not found — sidebar logo unchanged")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
