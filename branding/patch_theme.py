#!/usr/bin/env python3
"""Apply the Hanzo Studio black + purple theme to the prebuilt frontend.

Three orthogonal levers, each in its own lane:
  1. CSS shell skin   -> copy branding/hanzo-theme.css into static/ and inject a
                          <link> as the LAST stylesheet in index.html (idempotent).
  2. Canvas pixels    -> CSS cannot reach the litegraph <canvas>; rewrite the
                          "dark" palette's CLEAR_BACKGROUND_COLOR to pure black in
                          the JS bundle. Keyed off the stable palette id+name so it
                          replays across upgrades even if the value changes.

Usage: patch_theme.py <static_dir> <branding_dir>
"""
import os
import re
import shutil
import sys

STATIC_DIR = sys.argv[1]
BRANDING_DIR = sys.argv[2]

THEME_CSS = "hanzo-theme.css"
LINK_TAG = f'<link rel="stylesheet" href="{THEME_CSS}"/>'
PALETTE_ANCHOR = "id:`dark`,name:`Dark (Default)`"
PALETTE_SEGMENT_LEN = 1600


def install_css() -> None:
    src = os.path.join(BRANDING_DIR, THEME_CSS)
    dst = os.path.join(STATIC_DIR, THEME_CSS)
    shutil.copyfile(src, dst)
    print(f"  Installed {THEME_CSS}")


def inject_link() -> None:
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index):
        print("  WARNING: index.html not found — theme link not injected")
        return
    with open(index, encoding="utf-8") as f:
        html = f.read()
    if LINK_TAG in html:
        print("  Theme link already present in index.html")
        return
    if "</head>" not in html:
        print("  WARNING: no </head> in index.html — theme link not injected")
        return
    html = html.replace("</head>", LINK_TAG + "</head>", 1)
    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    print("  Injected hanzo-theme.css link into index.html")


def blacken_canvas() -> None:
    patched = 0
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
            if PALETTE_ANCHOR not in content:
                continue
            new_content = content
            for m in re.finditer(re.escape(PALETTE_ANCHOR), content):
                start = m.start()
                seg = new_content[start:start + PALETTE_SEGMENT_LEN]
                seg2 = re.sub(
                    r'CLEAR_BACKGROUND_COLOR:`#[0-9a-fA-F]{3,8}`',
                    "CLEAR_BACKGROUND_COLOR:`#000000`",
                    seg,
                    count=1,
                )
                if seg2 != seg:
                    new_content = new_content[:start] + seg2 + new_content[start + PALETTE_SEGMENT_LEN:]
            if new_content != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"  Blackened canvas clear color in {fname}")
                patched += 1
            elif "CLEAR_BACKGROUND_COLOR:`#000000`" in content:
                print(f"  Canvas clear color already black in {fname}")
                patched += 1
    if patched == 0:
        print("  WARNING: 'dark' palette not found — canvas color unchanged")


def main() -> None:
    install_css()
    inject_link()
    blacken_canvas()


if __name__ == "__main__":
    main()
