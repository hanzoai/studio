#!/usr/bin/env python3
"""Inject the Hanzo Studio deep-link opener into the prebuilt frontend.

Mirrors patch_copilot.py (same seam, same idempotency):
  1. Assets      -> copy hanzo-deeplink.js into static/.
  2. index.html  -> <script defer> as the last element before </body>, so
                    window.app exists when it reads the URL and opens/runs the
                    workflow.

Idempotent: strips any prior deeplink script (matched by filename) and re-injects
with a ?v=BUNDLE_VERSION cache-buster. Bump BUNDLE_VERSION when the JS changes.

Usage: patch_deeplink.py <static_dir> <branding_dir>
"""
import os
import re
import shutil
import sys

STATIC_DIR = sys.argv[1]
BRANDING_DIR = sys.argv[2]

BUNDLE_VERSION = "1"
JS = "hanzo-deeplink.js"
SCRIPT_TAG = f'<script src="{JS}?v={BUNDLE_VERSION}" defer></script>'


def install_assets() -> None:
    shutil.copyfile(os.path.join(BRANDING_DIR, JS), os.path.join(STATIC_DIR, JS))
    print(f"  Installed {JS}")


def inject() -> None:
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index):
        print("  WARNING: index.html not found — deep-link not injected")
        return
    with open(index, encoding="utf-8") as f:
        html = f.read()

    # Strip any prior deeplink tag (any ?v=) so re-runs stay clean.
    html = re.sub(r'<script[^>]*src="%s[^"]*"[^>]*></script>' % re.escape(JS), "", html)

    if "</body>" in html:
        html = html.replace("</body>", SCRIPT_TAG + "</body>", 1)
    else:  # no </body> (minified edge) — append; the script still runs
        html += SCRIPT_TAG

    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Injected deep-link script (v{BUNDLE_VERSION}) into index.html")


if __name__ == "__main__":
    install_assets()
    inject()
