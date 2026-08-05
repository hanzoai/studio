#!/usr/bin/env python3
"""
Smart frontend branding patcher for Hanzo Studio.
Replaces display strings and URLs without breaking JS code identifiers,
CSS class names, or dynamic import paths.
"""
import os
import re
import sys

STATIC_DIR = sys.argv[1]

# ============================================================
# URL replacements — safe to apply everywhere in all file types
# ============================================================
URL_REPLACEMENTS = [
    # Domains
    (r'https?://(?:www\.)?studio\.org', 'https://hanzo.ai'),
    (r'https?://docs\.studio\.org', 'https://docs.hanzo.ai'),
    (r'https?://forum\.studio\.org', 'https://hanzo.ai/community'),
    (r'https?://api\.studio\.org', 'https://api.hanzo.ai'),
    (r'https?://platform\.studio\.org', 'https://hanzo.ai'),
    (r'https?://media\.studio\.org', 'https://media.hanzo.ai'),
    # GitHub
    (r'github\.com/comfyanonymous/ComfyUI', 'github.com/hanzoai/studio'),
    (r'github\.com/Comfy-Org/ComfyUI', 'github.com/hanzoai/studio'),
    (r'github\.com/Comfy-Org', 'github.com/hanzoai'),
    (r'github\.com/comfyanonymous', 'github.com/hanzoai'),
    # Discord
    (r'discord\.gg/comfyorg', 'discord.gg/CJCyAsm9Vr'),
    (r'discord\.com/invite/comfyorg', 'discord.gg/CJCyAsm9Vr'),
    # Email
    (r'support@studio\.org', 'support@hanzo.ai'),
]

# ============================================================
# Display string replacements — only within quoted strings
# These are the user-visible text labels
# ============================================================
DISPLAY_REPLACEMENTS = [
    # Exact display strings — multi-word phrases are safe to replace globally
    # because they only appear as display text, not in code identifiers.
    # Order matters: longer matches first to avoid partial replacements.
    # Browser-tab title suffix: the frontend builds document.title as
    # `${workflowName} - ComfyUI`. The " - ComfyUI" constant has a space (not a
    # quote) before ComfyUI, so the quoted-string patterns below miss it.
    (' - ComfyUI', ' - Hanzo Studio'),
    ('ComfyUI Issues', 'Hanzo Studio Issues'),
    ('ComfyUI Docs', 'Hanzo Studio Docs'),
    ('ComfyUI Forum', 'Hanzo Studio Forum'),
    ('ComfyUI Version', 'Hanzo Studio Version'),
    ('ComfyUI Logo', 'Hanzo Studio Logo'),
    ('ComfyUI Settings', 'Hanzo Studio Settings'),
    ('About ComfyUI', 'About Hanzo Studio'),
    ('Open ComfyUI', 'Open Hanzo Studio'),
    ('Loading ComfyUI', 'Loading Hanzo Studio'),
    ('ComfyUI is already', 'Hanzo Studio is already'),
    ('ComfyUI server', 'Hanzo Studio server'),
    ('ComfyUI api', 'Hanzo Studio api'),
    ('ComfyUI: ', 'Hanzo Studio: '),
    ('ComfyUI version', 'Hanzo Studio version'),
    ('ComfyUI update', 'Hanzo Studio update'),
    # Comfy-Org/ComfyOrg: ONLY as multi-word display phrases (not bare)
    ('Comfy-Org Discord', 'Hanzo AI Discord'),
    ('ComfyOrg Discord', 'Hanzo AI Discord'),
    ('Comfy-Org Logo', 'Hanzo AI Logo'),
    ('ComfyOrg Logo', 'Hanzo AI Logo'),
    ('Comfy Org', 'Hanzo AI'),
    # studio.org domain in display text (not URLs — those are handled by URL_REPLACEMENTS)
    ('studio.org', 'hanzo.ai'),
    # Comfy Cloud branding
    ('Comfy Cloud Logo', 'Hanzo Cloud Logo'),
    ('Comfy Cloud', 'Hanzo Cloud'),
    # Catch standalone "ComfyUI" in display contexts not caught above
    ('Welcome to ComfyUI', 'Welcome to Hanzo Studio'),
    ('Powered by ComfyUI', 'Powered by Hanzo Studio'),
    ('ComfyUI Desktop', 'Hanzo Studio Desktop'),
    ('ComfyUI Manager', 'Hanzo Studio Manager'),
    ('ComfyUI Community', 'Hanzo Studio Community'),
    ('ComfyUI Workflow', 'Hanzo Studio Workflow'),
    ('ComfyUI Node', 'Hanzo Studio Node'),
    ('ComfyUI Extension', 'Hanzo Studio Extension'),
    ('ComfyUI Custom', 'Hanzo Studio Custom'),
    ('ComfyUI API', 'Hanzo Studio API'),
    ('ComfyUI Python', 'Hanzo Studio Python'),
    ('ComfyUI frontend', 'Hanzo Studio frontend'),
    ('ComfyUI Backend', 'Hanzo Studio Backend'),
    ('comfyui.com', 'hanzo.ai'),
    ('comfy.org', 'hanzo.ai'),
]

# ============================================================
# i18n label-value replacements — for translation value strings
# Pattern: label:`...` or label:"..." or "key":"value"
# ============================================================
I18N_LABEL_PATTERNS = [
    # In i18n bundles, display values appear as: label:`text` or {label:"text"}
    (r'(label:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(label:\s*[`"\'])Comfy-Org', r'\1Hanzo AI'),
    (r'(label:\s*[`"\'])ComfyOrg', r'\1Hanzo AI'),
    # Title patterns
    (r'(title:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(title:\s*[`"\'])Comfy-Org', r'\1Hanzo AI'),
    # Alt text
    (r'(Alt:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(Alt:\s*[`"\'])Comfy\b', r'\1Hanzo'),
    # Description patterns
    (r'(description:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    # i18n value strings: `"key":"ComfyUI ..."` or `key:\`ComfyUI ...\``
    # Match ComfyUI at the start of a value after a colon+quote
    (r'(:\s*[`"\'])ComfyUI ([A-Z])', r'\1Hanzo Studio \2'),
    # Standalone quoted "ComfyUI" as a value (not part of a longer identifier)
    (r'([`"\'])ComfyUI([`"\'])', r'\1Hanzo Studio\2'),
    # "Comfy-Org" as standalone quoted value
    (r'([`"\'])Comfy-Org([`"\'])', r'\1Hanzo AI\2'),
    (r'([`"\'])ComfyOrg([`"\'])', r'\1Hanzo AI\2'),
    # Match display strings like: "ComfyUI Docs":`Hanzo Studio Docs`
    (r'([`"\'])ComfyUI (Issues|Docs|Forum|Logo|Settings|Version)', r'\1Hanzo Studio \2'),
    (r'([`"\'])Comfy-Org (Discord|Logo)', r'\1Hanzo AI \2'),
    (r'([`"\'])ComfyOrg (Discord|Logo)', r'\1Hanzo AI \2'),
]

# ============================================================
# Quoted string replacements — replace within backtick/quote contexts
# These catch display text that appears in template literals
# ============================================================
QUOTED_DISPLAY_PATTERNS = [
    # "ComfyUI ..." within string literals (template, double, single quotes)
    # Match: `...ComfyUI...` or "...ComfyUI..." (display text in strings)
    # Use lookbehind for quote chars and common preceding text patterns
    (r'(?<=`)ComfyUI(?=[` ])', 'Hanzo Studio'),
    (r'(?<=")ComfyUI(?=[" ])', 'Hanzo Studio'),
    (r"(?<=')ComfyUI(?=[' ])", 'Hanzo Studio'),
    # "Comfy Cloud" → "Hanzo Cloud" in display strings
    (r'(?<=[`"\'])Comfy Cloud(?=[`"\' ,.])', 'Hanzo Cloud'),
]


def patch_file(filepath: str) -> bool:
    """Patch a single file. Returns True if modified."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return False

    original = content

    # 1. URL replacements (safe everywhere)
    for pattern, replacement in URL_REPLACEMENTS:
        content = re.sub(pattern, replacement, content)

    # 2. Display string replacements (plain text substitution)
    for old, new in DISPLAY_REPLACEMENTS:
        content = content.replace(old, new)

    # 3. i18n label patterns (regex)
    for pattern, replacement in I18N_LABEL_PATTERNS:
        content = re.sub(pattern, replacement, content)

    # 4. Quoted display patterns
    for pattern, replacement in QUOTED_DISPLAY_PATTERNS:
        content = re.sub(pattern, replacement, content)

    # 5. Clean up any double replacements
    content = content.replace('Hanzo Studio Studio', 'Hanzo Studio')
    content = content.replace('Hanzo AI AI', 'Hanzo AI')

    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    return False


def main():
    static_dir = STATIC_DIR
    patched = 0
    total = 0

    # Walk ALL files under static dir
    for root, dirs, files in os.walk(static_dir):
        for fname in files:
            if not fname.endswith(('.js', '.css', '.html', '.json')):
                continue
            total += 1
            filepath = os.path.join(root, fname)
            if patch_file(filepath):
                patched += 1

    print(f"  Scanned {total} files, patched {patched}")

    # Verification: count remaining "ComfyUI" in display contexts
    remaining = 0
    for root, dirs, files in os.walk(static_dir):
        for fname in files:
            if not fname.endswith(('.js', '.html', '.json')):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                # Count display-context ComfyUI (in quoted strings)
                hits = len(re.findall(r'[`"\']ComfyUI[`"\' ]', content))
                remaining += hits
            except Exception:
                pass

    if remaining > 0:
        print(f"  Warning: {remaining} display-context 'ComfyUI' references remain")
    else:
        print("  All display-context 'ComfyUI' references replaced")


if __name__ == '__main__':
    main()
