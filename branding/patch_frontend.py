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
    (r'https?://(?:www\.)?comfy\.org', 'https://hanzo.ai'),
    (r'https?://docs\.comfy\.org', 'https://docs.hanzo.ai'),
    (r'https?://forum\.comfy\.org', 'https://hanzo.ai/community'),
    (r'https?://api\.comfy\.org', 'https://api.hanzo.ai'),
    (r'https?://platform\.comfy\.org', 'https://hanzo.ai'),
    (r'https?://media\.comfy\.org', 'https://media.hanzo.ai'),
    # GitHub
    (r'github\.com/comfyanonymous/ComfyUI', 'github.com/hanzoai/studio'),
    (r'github\.com/Comfy-Org/ComfyUI', 'github.com/hanzoai/studio'),
    (r'github\.com/Comfy-Org', 'github.com/hanzoai'),
    (r'github\.com/comfyanonymous', 'github.com/hanzoai'),
    # Discord
    (r'discord\.gg/comfyorg', 'discord.gg/hanzoai'),
    (r'discord\.com/invite/comfyorg', 'discord.com/invite/hanzoai'),
    # Email
    (r'support@comfy\.org', 'support@hanzo.ai'),
]

# ============================================================
# Display string replacements — only within quoted strings
# These are the user-visible text labels
# ============================================================
DISPLAY_REPLACEMENTS = [
    # Exact display strings (applied within string literals only)
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
    ('Comfy-Org Discord', 'Hanzo AI Discord'),
    ('ComfyOrg Discord', 'Hanzo AI Discord'),
    ('Comfy-Org', 'Hanzo AI'),
    ('ComfyOrg Logo', 'Hanzo AI Logo'),
    ('ComfyOrg', 'Hanzo AI'),
    ('Comfy Org', 'Hanzo AI'),
    ('comfy.org', 'hanzo.ai'),
]

# ============================================================
# i18n label-value replacements — for translation value strings
# Pattern: label:`...` or label:"..." or "key":"value"
# ============================================================
I18N_LABEL_PATTERNS = [
    # In i18n bundles, display values appear as: label:`text`
    (r'(label:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(label:\s*[`"\'])Comfy-Org', r'\1Hanzo AI'),
    (r'(label:\s*[`"\'])ComfyOrg', r'\1Hanzo AI'),
    # Title patterns
    (r'(title:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(title:\s*[`"\'])Comfy-Org', r'\1Hanzo AI'),
    # Alt text
    (r'(Alt:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
    (r'(Alt:\s*[`"\'])Comfy', r'\1Hanzo'),
    # Description patterns
    (r'(description:\s*[`"\'])ComfyUI', r'\1Hanzo Studio'),
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
