#!/usr/bin/env python3
"""De-Comfy the served English frontend: user-visible 'ComfyUI'/'Comfy' brand
words -> 'Hanzo Studio'/'Studio', without touching code identifiers, i18n keys,
setting keys, or the non-English locale bundles.

One concern only: the PRODUCT brand word in English UI copy. (URLs/domains and
the org name 'Comfy-Org'->'Hanzo AI' stay in patch_frontend.py; logos/theme
elsewhere.) Runs after patch_frontend.py in apply-branding.sh and mops up every
mid-string 'ComfyUI' the curated phrase list there structurally cannot reach
(e.g. `Update ComfyUI`, `Installing ComfyUI`), plus the Settings dialog section
headers.

Two rules:

  1. Whole-word 'ComfyUI' -> 'Hanzo Studio', but only where it reads as a word in
     a display string: preceded by open-quote / space / '>' / '(' and followed by
     close-quote / space / sentence punctuation. That boundary skips every code
     use found in the bundle — ComfyUIStarted, .ComfyUI, =ComfyUI, the
     /^(ComfyUI-.../ regex, and dotted i18n keys like .category.ComfyUI (the '.'
     is not an opening delimiter) — and hyphenated product names (ComfyUI-Manager,
     the '-' is not a closing delimiter) so the actual third-party tool keeps its
     name.

  2. Curated bare-'Comfy' *values*, replaced as exact key:`value` pairs so the
     setting KEYS ('Comfy', 'Comfy-Desktop', 'Comfy.ColorPalette', which the app
     reads programmatically) are preserved while the displayed section header
     becomes 'Studio' / 'Studio Desktop'. Plus 'Comfy Cloud' -> 'Hanzo Cloud'.

Scope guard: English only. A file is processed iff its non-ASCII byte ratio is
below LOCALE_RATIO (English bundles ~0.02%, every locale bundle >=0.31%), and
each individual match is additionally required to sit in a pure-ASCII window so a
stray CJK/RTL run is never rewritten. Idempotent: a second run finds no
display-context 'ComfyUI' and the exact value pairs are already rewritten.

Usage: patch_destring.py <static_dir>
"""
import os
import re
import sys

STATIC_DIR = sys.argv[1]

# A file is treated as a non-English locale bundle (skipped) above this ratio.
# English bundles measure ~0.0002; the lowest locale bundle ~0.0031.
LOCALE_RATIO = 0.0015
ASCII_WINDOW = 24  # chars of context each side that must be ASCII to rewrite

WORD_RE = re.compile(r"(?<=[\s`\"'>(])ComfyUI(?=[\s`\"'.,!?:;<)])")
WORD_REPL = "Hanzo Studio"

# Exact key:`value` pairs — key preserved, display value rebranded.
CURATED = [
    ("Comfy:`Comfy`", "Comfy:`Studio`"),                              # settingsCategories header
    ('"Comfy-Desktop":`Comfy-Desktop`', '"Comfy-Desktop":`Studio Desktop`'),
    ("comfy:`Comfy`", "comfy:`Studio`"),
    ("Comfy Cloud", "Hanzo Cloud"),
]

EXTS = (".js", ".html", ".json")


def _non_ascii_ratio(raw: bytes) -> float:
    if not raw:
        return 1.0
    return sum(1 for c in raw if c > 127) / len(raw)


def _replace_word(text: str) -> tuple[str, int]:
    """Apply WORD_RE, but only where the surrounding window is pure ASCII."""
    out, last, n = [], 0, 0
    for mobj in WORD_RE.finditer(text):
        s, e = mobj.start(), mobj.end()
        ctx = text[max(0, s - ASCII_WINDOW):e + ASCII_WINDOW]
        out.append(text[last:s])
        if all(ord(c) < 128 for c in ctx):
            out.append(WORD_REPL)
            n += 1
        else:
            out.append(mobj.group(0))
        last = e
    out.append(text[last:])
    return "".join(out), n


def patch_file(path: str) -> int:
    raw = open(path, "rb").read()
    if _non_ascii_ratio(raw) > LOCALE_RATIO:
        return 0  # non-English locale bundle — out of scope
    text = raw.decode("utf-8", errors="ignore")
    original = text

    text, changed = _replace_word(text)
    for old, new in CURATED:
        if old in text:
            changed += text.count(old)
            text = text.replace(old, new)

    # Collapse any accidental doubling from overlapping prior passes.
    text = text.replace("Hanzo Studio Studio", "Hanzo Studio").replace("Hanzo Cloud Cloud", "Hanzo Cloud")

    if text != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return changed


def _display_count(static_dir: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(static_dir):
        for fn in files:
            if not fn.endswith(EXTS):
                continue
            p = os.path.join(root, fn)
            raw = open(p, "rb").read()
            if _non_ascii_ratio(raw) > LOCALE_RATIO:
                continue
            total += len(WORD_RE.findall(raw.decode("utf-8", errors="ignore")))
    return total


def main() -> None:
    before = _display_count(STATIC_DIR)
    files = changed = 0
    for root, _dirs, names in os.walk(STATIC_DIR):
        for fn in names:
            if not fn.endswith(EXTS):
                continue
            n = patch_file(os.path.join(root, fn))
            if n:
                files += 1
                changed += n
    after = _display_count(STATIC_DIR)
    print(f"  De-Comfy: {changed} display strings across {files} English files "
          f"(display 'ComfyUI' {before} -> {after})")


if __name__ == "__main__":
    main()
