#!/usr/bin/env python3
"""
Seed the default multi-tab fashion workspace for Hanzo Studio.

Injects a tiny <head> bootstrap script into index.html that pre-populates the
localStorage keys the frontend reads on boot to restore open workflow tabs:

  Comfy.OpenWorkflowsPaths   JSON array of workflow paths (one tab each)
  Comfy.ActiveWorkflowIndex  restore split index (0 = open all as trailing tabs)

Why localStorage: the frontend's restore path (restoreWorkflowTabsState in
GraphView useWorkflowPersistence) reads these through getStorageValue()/
setStorageValue(), which are localStorage helpers with a clientId-suffixed
sessionStorage layer read FIRST. On re-seed we therefore also purge any stale
`<key>:<clientId>` sessionStorage shadows so the fresh localStorage seed is
authoritative even on a same-tab reload (otherwise a previously-saved 1-tab
shadow would mask the seed).

ComfyUI always opens its own blank "Unsaved Workflow" as the active tab on boot
(initializeWorkflow -> loadDefaultWorkflow); the 25 seeded fashion workflows are
restored alongside it in the background, so the workspace opens with 26 tabs.
There is no seed key that suppresses the default tab: loadPersistedWorkflow only
rehydrates drafts / the last-edited graph blob, never a userdata file by path.

The seed runs at most once per browser profile, guarded by hanzoWorkspaceSeed.
Bump SEED_VERSION to force every existing profile to re-seed exactly once.

Idempotent: re-running strips the previous seed <script> and re-injects the
current one, so this is safe to call on every branding pass.
"""
import re
import sys

STATIC_DIR = sys.argv[1]
INDEX = f"{STATIC_DIR}/index.html"

SEED_VERSION = "v3"

# Curated 8-design x 3-pose matrix + the shared 4K upscaler = 25 tabs.
DESIGNS = [
    "festival_fuchsia", "neptune_green", "floralmesh", "stripedgem",
    "redonepiece", "valentina", "red", "ches",
]
POSES = ["product", "hover", "shoot"]
PATHS = [f"workflows/fashion/{pose}_{d}.json" for d in DESIGNS for pose in POSES]
PATHS.append("workflows/fashion/upscale_4k.json")

# Marker id lets us find & replace our own block idempotently.
MARKER = "hanzo-workspace-seed"

paths_json = "[" + ", ".join('"%s"' % p for p in PATHS) + "]"

SEED_SCRIPT = (
    f'<script id="{MARKER}">try{{'
    f"if(localStorage.getItem('hanzoWorkspaceSeed')!=='{SEED_VERSION}'){{"
    f"localStorage.setItem('Comfy.OpenWorkflowsPaths',JSON.stringify({paths_json}));"
    f"localStorage.setItem('Comfy.ActiveWorkflowIndex','0');"
    f"for(var i=sessionStorage.length-1;i>=0;i--){{var k=sessionStorage.key(i);"
    f"if(k&&(k.indexOf('Comfy.OpenWorkflowsPaths:')===0"
    f"||k.indexOf('Comfy.ActiveWorkflowIndex:')===0))sessionStorage.removeItem(k);}}"
    f"localStorage.setItem('hanzoWorkspaceSeed','{SEED_VERSION}');"
    f"}}}}catch(e){{}}</script>"
)


def main() -> None:
    with open(INDEX, "r", encoding="utf-8") as f:
        html = f.read()

    # Remove any prior seed block (marked or the original unmarked variant).
    html = re.sub(r'<script id="%s">.*?</script>' % re.escape(MARKER), "", html,
                  flags=re.DOTALL)
    html = re.sub(r"<script>try\{if\(localStorage\.getItem\('hanzoWorkspaceSeed'\).*?</script>",
                  "", html, flags=re.DOTALL)

    if MARKER not in html:
        html = html.replace("<head>", "<head>" + SEED_SCRIPT, 1)

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  Seeded workspace ({len(PATHS)} tabs, guard {SEED_VERSION}) into index.html")


if __name__ == "__main__":
    main()
