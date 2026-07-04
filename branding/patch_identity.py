#!/usr/bin/env python3
"""Inject the Hanzo Studio Identity menu into the prebuilt frontend.

Mirrors patch_copilot.py exactly (same two orthogonal levers):
  1. Assets  -> copy hanzo-identity.js + hanzo-identity.css into static/.
  2. index.html seam ->
       - <link> for the CSS as the last stylesheet in <head>
       - <script defer> for the bundle as the last element before </body>.

Idempotent: strips any prior identity link/script (matched by filename) and
re-injects with a ?v=BUNDLE_VERSION cache-buster. Bump BUNDLE_VERSION when the
JS/CSS changes so browsers pick it up on the next load.

Usage: patch_identity.py <static_dir> <branding_dir>
"""
import os
import re
import shutil
import sys

STATIC_DIR = sys.argv[1]
BRANDING_DIR = sys.argv[2]

BUNDLE_VERSION = "1"
JS = "hanzo-identity.js"
CSS = "hanzo-identity.css"
LINK_TAG = f'<link rel="stylesheet" href="{CSS}?v={BUNDLE_VERSION}"/>'
SCRIPT_TAG = f'<script src="{JS}?v={BUNDLE_VERSION}" defer></script>'


def install_assets() -> None:
    for fname in (JS, CSS):
        shutil.copyfile(os.path.join(BRANDING_DIR, fname), os.path.join(STATIC_DIR, fname))
    print(f"  Installed {JS} + {CSS}")


def inject() -> None:
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index):
        print("  WARNING: index.html not found — identity menu not injected")
        return
    with open(index, encoding="utf-8") as f:
        html = f.read()

    html = re.sub(r'<link[^>]*href="%s[^"]*"[^>]*>' % re.escape(CSS), "", html)
    html = re.sub(r'<script[^>]*src="%s[^"]*"[^>]*></script>' % re.escape(JS), "", html)

    if "</head>" in html:
        html = html.replace("</head>", LINK_TAG + "</head>", 1)
    if "</body>" in html:
        html = html.replace("</body>", SCRIPT_TAG + "</body>", 1)
    else:
        html += SCRIPT_TAG

    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Injected identity link + script (v{BUNDLE_VERSION}) into index.html")


def main() -> None:
    install_assets()
    inject()


if __name__ == "__main__":
    main()
