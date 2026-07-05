#!/usr/bin/env python3
"""Force ComfyUI "Nodes 2.0" (Vue/DOM node rendering, the `Comfy.VueNodes.Enabled`
setting) ON for the cloud build and remove it as a user-facing choice.

Upstream ships Nodes 2.0 as an *experimental, default-off* toggle exposed in two
places: a row in the Settings dialog (category Comfy > "Nodes 2.0") and a switch
in the top-left logo menu (analytics id `menu_nodes_2.0_toggle`). studio.hanzo.ai
ships the modern node design as the one and only node renderer, so this patch:

  1. Flips the registered default of `Comfy.VueNodes.Enabled` !1 -> !0 (on).
  2. Sets its `type` boolean -> hidden, so the Settings dialog stops rendering the
     row (ComfyUI's own mechanism — same as `Comfy.NodeLibrary.Bookmarks.V2`).
     Same hidden treatment for the sibling `Comfy.VueNodes.AutoScaleLayout` so the
     entire "Nodes 2.0" settings category collapses (no orphan row). Its default
     is already !0 upstream.
  3. Flips every inline read fallback `...Enabled`)??!1` -> `??!0`, so a fresh
     profile (no persisted value) resolves to on at every call site.
  4. Deletes the `nodes-2.0-toggle` item from the logo-menu model so the menu
     switch is gone too.

Operates on the served frontend bundle (comfyui-frontend-package/static), anchored
on the setting id / menu-item literal (not on minified hashes), so it survives a
frontend bump as long as the setting keeps its id. Idempotent: after a run the
`boolean`/`!1`/menu-item strings for these settings no longer exist, so a second
run is a no-op. Prints what it changed.

Usage: patch_vuenodes.py <static_dir>
"""
import os
import re
import sys

STATIC_DIR = sys.argv[1]

# Registration objects to force-on + hide. Anchored on the setting id and bounded
# to that one object by the lazy run up to its `versionAdded`.
FORCE_ON = "Comfy.VueNodes.Enabled"          # experimental default-off -> on
HIDE_ONLY = "Comfy.VueNodes.AutoScaleLayout"  # already default-on -> just hide row

# Logo-menu item literal (top-left menu switch). Removing it drops the switch.
MENU_ITEM = "{key:`nodes-2.0-toggle`,label:`Nodes 2.0`},"


def _reg_pattern(setting_id: str) -> re.Pattern:
    # id:`<setting_id>` ... versionAdded:`x.y.z`  (one registration object)
    return re.compile(
        r"id:`" + re.escape(setting_id) + r"`.{0,500}?versionAdded:`[^`]*`",
        re.S,
    )


def patch_text(text: str) -> tuple[str, int]:
    n = 0

    def force_on(m: re.Match) -> str:
        seg = m.group(0)
        seg = seg.replace("type:`boolean`", "type:`hidden`")
        seg = seg.replace("defaultValue:!1", "defaultValue:!0")
        return seg

    def hide_only(m: re.Match) -> str:
        return m.group(0).replace("type:`boolean`", "type:`hidden`")

    text, c = _reg_pattern(FORCE_ON).subn(force_on, text)
    n += c
    text, c = _reg_pattern(HIDE_ONLY).subn(hide_only, text)
    n += c

    # Inline read fallbacks: unset -> on.
    needle = FORCE_ON + "`)??!1"
    if needle in text:
        n += text.count(needle)
        text = text.replace(needle, FORCE_ON + "`)??!0")

    # Remove the logo-menu switch item.
    if MENU_ITEM in text:
        n += text.count(MENU_ITEM)
        text = text.replace(MENU_ITEM, "")

    return text, n


def main() -> None:
    files = changed = 0
    for root, _dirs, names in os.walk(os.path.join(STATIC_DIR, "assets")):
        for fn in names:
            if not fn.endswith(".js"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8", errors="ignore") as f:
                original = f.read()
            text, n = patch_text(original)
            if n and text != original:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                files += 1
                changed += n
    print(f"  Nodes 2.0 forced on + toggles removed: {changed} edits across {files} files")


if __name__ == "__main__":
    main()
