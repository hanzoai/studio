#!/usr/bin/env python3
"""De-Comfy display strings: user-visible 'ComfyUI'/'Comfy' brand words ->
'Hanzo Studio'/'Studio', in every language, without touching code identifiers,
i18n keys or setting keys.

One concern only: the PRODUCT brand word in UI copy. (URLs/domains and the org
name 'Comfy-Org'->'Hanzo AI' stay in patch_frontend.py; logos/theme elsewhere.)
Runs after patch_frontend.py in apply-branding.sh over both trees the user reads
— the frontend bundle and the embedded node docs — and mops up every mid-string
'ComfyUI' the curated phrase list there structurally cannot reach (e.g. `Update
ComfyUI`, `Installing ComfyUI`), plus the Settings dialog section headers.

Two rules:

  1. Whole-word 'ComfyUI' -> 'Hanzo Studio', but only where it reads as a word in
     a display string: opened by quote / space / '>' / '(' and closed by quote /
     space / sentence punctuation, or bounded on either side by any non-ASCII
     character. That boundary skips every code use in the bundle — ComfyUIStarted,
     .ComfyUI, =ComfyUI, the /^(ComfyUI-.../ regex, dotted i18n keys like
     .category.ComfyUI (the '.' is not an opener), minified identifiers such as
     Comfy_Help_OpenComfyUIDocs — and hyphenated product names (ComfyUI-Manager,
     the '-' is not a closer) so the third-party tool keeps its name.

     Non-ASCII is a delimiter because that alone is what carries the rule out of
     English. Translated copy butts the brand straight against its own script
     with no separating space, so an ASCII-only boundary matched nothing at all
     in a locale bundle: `ComfyUIサーバーを起動中...`, `ComfyUI서버`, `ComfyUIكخادم`.

  2. Curated bare-'Comfy' *values*, replaced as exact key:`value` pairs so the
     setting KEYS ('Comfy', 'Comfy-Desktop', 'Comfy.ColorPalette', which the app
     reads programmatically) are preserved while the displayed section header
     becomes 'Studio' / 'Studio Desktop'. Plus 'Comfy Cloud' -> 'Hanzo Cloud'.

Left alone deliberately, in every language: path examples ('Documents/ComfyUI',
the "audio/ComfyUI" filename prefix in the node docs) — '/' is not an opener, and
those name a directory rather than the product. Idempotent: a second run finds no
display-context 'ComfyUI' and the exact value pairs are already rewritten.

Usage: patch_destring.py <dir>   # the frontend static tree, or the node docs
"""
import os
import re
import sys

ROOT = sys.argv[1]

# A display word is bounded by ASCII quote/space/punctuation, or by any
# non-ASCII character (see rule 1 above). '*' and '_' are boundaries because the
# node docs are markdown and write the brand emphasised -- `**ComfyUI**` -- which
# an otherwise-correct rule reads as unbounded and skips. That form carries most
# of the remaining brand in the translated docs.
OPEN = r"[\s`\"'>(*_]|[^\x00-\x7f]"
CLOSE = r"[\s`\"'.,!?:;<)*_]|[^\x00-\x7f]"

WORD_RE = re.compile(rf"(?<={OPEN})ComfyUI(?={CLOSE})")
WORD_REPL = "Hanzo Studio"

# Exact key:`value` pairs — key preserved, display value rebranded.
CURATED = [
    ("Comfy:`Comfy`", "Comfy:`Studio`"),                              # settingsCategories header
    ('"Comfy-Desktop":`Comfy-Desktop`', '"Comfy-Desktop":`Studio Desktop`'),
    ("comfy:`Comfy`", "comfy:`Studio`"),
    ("Comfy Cloud", "Hanzo Cloud"),
]

EXTS = (".js", ".html", ".json", ".md")


def _files(root: str):
    for base, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(EXTS):
                yield os.path.join(base, name)


def patch_file(path: str) -> int:
    original = open(path, encoding="utf-8").read()

    text, changed = WORD_RE.subn(WORD_REPL, original)
    for old, new in CURATED:
        changed += text.count(old)
        text = text.replace(old, new)

    # Collapse any accidental doubling from overlapping prior passes.
    text = text.replace("Hanzo Studio Studio", "Hanzo Studio").replace("Hanzo Cloud Cloud", "Hanzo Cloud")

    if text != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return changed


def _display_count(root: str) -> int:
    return sum(len(WORD_RE.findall(open(p, encoding="utf-8").read())) for p in _files(root))


def main() -> None:
    before = _display_count(ROOT)
    files = changed = 0
    for path in _files(ROOT):
        n = patch_file(path)
        if n:
            files += 1
            changed += n
    after = _display_count(ROOT)
    print(f"  De-Comfy {os.path.basename(ROOT.rstrip('/'))}: {changed} display strings across "
          f"{files} files (display 'ComfyUI' {before} -> {after})")


if __name__ == "__main__":
    main()
