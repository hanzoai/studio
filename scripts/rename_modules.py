#!/usr/bin/env python3
"""
Comprehensive module rename: studio* → studio*

This script renames all internal Python modules from the ComfyUI naming
convention to Hanzo Studio naming convention.

Renames:
  studio/           → studio/
  studio_extras/    → studio_extras/
  studio_api/       → studio_api/
  studio_api_nodes/ → studio_api_nodes/
  studio_config/    → studio_config/
  studio_execution/ → studio_execution/
  studio_version.py → studio_version.py

  tests-unit/studio_api_test/    → tests-unit/studio_api_test/
  tests-unit/studio_extras_test/ → tests-unit/studio_extras_test/
  tests-unit/studio_quant/       → tests-unit/studio_quant/
  tests-unit/studio_test/        → tests-unit/studio_test/

Does NOT rename external pip packages:
  comfyui-frontend-package, comfyui-workflow-templates,
  comfyui-embedded-docs, comfyui_manager, comfy-kitchen, comfy-aimdo
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Directory renames (old_name → new_name)
DIR_RENAMES = {
    "studio": "studio",
    "studio_extras": "studio_extras",
    "studio_api": "studio_api",
    "studio_api_nodes": "studio_api_nodes",
    "studio_config": "studio_config",
    "studio_execution": "studio_execution",
}

# Test directory renames
TEST_DIR_RENAMES = {
    "tests-unit/studio_api_test": "tests-unit/studio_api_test",
    "tests-unit/studio_extras_test": "tests-unit/studio_extras_test",
    "tests-unit/studio_quant": "tests-unit/studio_quant",
    "tests-unit/studio_test": "tests-unit/studio_test",
}

# File renames
FILE_RENAMES = {
    "studio_version.py": "studio_version.py",
}

# External pip packages — NEVER rename these imports
EXTERNAL_PACKAGES = {
    "comfyui_frontend_package",
    "comfyui_workflow_templates",
    "comfyui_embedded_docs",
    "comfyui_manager",
    "comfy_kitchen",
    "comfy_aimdo",
}

# Pip package names in requirements files (hyphenated form)
EXTERNAL_PIP_NAMES = {
    "comfyui-frontend-package",
    "comfyui-workflow-templates",
    "comfyui-embedded-docs",
    "comfyui_manager",
    "comfy-kitchen",
    "comfy-aimdo",
}

# Internal module names that should be renamed
INTERNAL_MODULES = [
    # Order matters - longer prefixes first to avoid partial matches
    "studio_api_nodes",
    "studio_api_test",
    "studio_extras_test",
    "studio_extras",
    "studio_execution",
    "studio_config",
    "studio_quant",
    "studio_test",
    "node_types",  # submodule of studio → stays as studio.node_types? No - it's studio/node_types/ → studio/node_types/ which is fine
    "studio_version",
    "studio_api",
    "studio",
]

# Corresponding new names
MODULE_RENAME_MAP = {
    "studio_api_nodes": "studio_api_nodes",
    "studio_api_test": "studio_api_test",
    "studio_extras_test": "studio_extras_test",
    "studio_extras": "studio_extras",
    "studio_execution": "studio_execution",
    "studio_config": "studio_config",
    "studio_quant": "studio_quant",
    "studio_test": "studio_test",
    "studio_version": "studio_version",
    "studio_api": "studio_api",
    "studio": "studio",
}

# File extensions to process for import updates
PYTHON_EXTENSIONS = {".py", ".pyi"}
TEXT_EXTENSIONS = {".py", ".pyi", ".yml", ".yaml", ".toml", ".cfg", ".ini",
                   ".md", ".txt", ".sh", ".bat", ".cmd", ".ps1", ".json"}

# ==============================================================================
# PHASE 1: Git mv directories and files
# ==============================================================================

def phase1_git_mv():
    """Rename directories and files using git mv."""
    print("\n=== PHASE 1: Git mv directories and files ===")

    for old, new in DIR_RENAMES.items():
        if Path(old).exists():
            print(f"  git mv {old}/ → {new}/")
            subprocess.run(["git", "mv", old, new], check=True)
        else:
            print(f"  SKIP {old}/ (not found)")

    for old, new in TEST_DIR_RENAMES.items():
        if Path(old).exists():
            print(f"  git mv {old}/ → {new}/")
            subprocess.run(["git", "mv", old, new], check=True)
        else:
            print(f"  SKIP {old}/ (not found)")

    for old, new in FILE_RENAMES.items():
        if Path(old).exists():
            print(f"  git mv {old} → {new}")
            subprocess.run(["git", "mv", old, new], check=True)
        else:
            print(f"  SKIP {old} (not found)")

# ==============================================================================
# PHASE 2: Update Python imports
# ==============================================================================

def is_external_import(line: str) -> bool:
    """Check if an import line references an external package."""
    for pkg in EXTERNAL_PACKAGES:
        # Match: import comfy_kitchen, from comfy_kitchen import, etc.
        if re.search(rf'\b{re.escape(pkg)}\b', line):
            return True
    return False


def rename_module_in_string(s: str) -> str:
    """Rename studio module references in a string, preserving external packages."""
    result = s

    # First, protect external package references by replacing them with placeholders
    placeholders = {}
    for i, pkg in enumerate(sorted(EXTERNAL_PACKAGES, key=len, reverse=True)):
        placeholder = f"__EXTERNAL_PKG_{i}__"
        placeholders[placeholder] = pkg
        result = result.replace(pkg, placeholder)

    # Also protect pip-style names
    pip_placeholders = {}
    for i, pkg in enumerate(sorted(EXTERNAL_PIP_NAMES, key=len, reverse=True)):
        placeholder = f"__EXTERNAL_PIP_{i}__"
        pip_placeholders[placeholder] = pkg
        result = result.replace(pkg, placeholder)

    # Now do the renames (longest first to avoid partial matches)
    for old, new in sorted(MODULE_RENAME_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        # Word-boundary aware replacement
        result = re.sub(rf'\b{re.escape(old)}\b', new, result)

    # Restore external package references
    for placeholder, pkg in placeholders.items():
        result = result.replace(placeholder, pkg)
    for placeholder, pkg in pip_placeholders.items():
        result = result.replace(placeholder, pkg)

    return result


def phase2_update_python_imports():
    """Update all Python import statements."""
    print("\n=== PHASE 2: Update Python imports ===")

    count = 0
    # Find all Python files
    py_files = []
    for ext in PYTHON_EXTENSIONS:
        py_files.extend(ROOT.rglob(f"*{ext}"))

    # Exclude .git, node_modules, __pycache__, .venv
    py_files = [f for f in py_files if not any(
        part.startswith('.') or part in ('node_modules', '__pycache__', '.venv', 'venv')
        for part in f.relative_to(ROOT).parts
    )]

    for filepath in py_files:
        try:
            content = filepath.read_text(encoding='utf-8')
        except (UnicodeDecodeError, PermissionError):
            continue

        new_content = rename_module_in_string(content)

        if new_content != content:
            filepath.write_text(new_content, encoding='utf-8')
            count += 1
            rel = filepath.relative_to(ROOT)
            print(f"  Updated: {rel}")

    print(f"  Total Python files updated: {count}")


# ==============================================================================
# PHASE 3: Update non-Python files
# ==============================================================================

def phase3_update_other_files():
    """Update references in non-Python files (YAML, TOML, MD, etc.)."""
    print("\n=== PHASE 3: Update non-Python files ===")

    count = 0
    all_files = []
    for ext in TEXT_EXTENSIONS - PYTHON_EXTENSIONS:
        all_files.extend(ROOT.rglob(f"*{ext}"))

    # Also check specific files
    for name in ["Dockerfile", "Makefile", ".dockerignore", ".gitignore",
                 "CODEOWNERS", "alembic.ini"]:
        p = ROOT / name
        if p.exists():
            all_files.append(p)

    # Exclude .git, node_modules, etc.
    all_files = [f for f in all_files if not any(
        part.startswith('.git') or part in ('node_modules', '__pycache__', '.venv', 'venv')
        for part in f.relative_to(ROOT).parts
    )]
    # But include .github
    all_files_filtered = []
    for f in all_files:
        parts = f.relative_to(ROOT).parts
        skip = False
        for part in parts:
            if part.startswith('.') and part not in ('.github', '.ci'):
                skip = True
                break
            if part in ('node_modules', '__pycache__', '.venv', 'venv'):
                skip = True
                break
        if not skip:
            all_files_filtered.append(f)

    # Deduplicate
    all_files_filtered = list(set(all_files_filtered))

    for filepath in all_files_filtered:
        try:
            content = filepath.read_text(encoding='utf-8')
        except (UnicodeDecodeError, PermissionError):
            continue

        new_content = rename_module_in_string(content)

        if new_content != content:
            filepath.write_text(new_content, encoding='utf-8')
            count += 1
            rel = filepath.relative_to(ROOT)
            print(f"  Updated: {rel}")

    print(f"  Total non-Python files updated: {count}")


# ==============================================================================
# PHASE 4: Create compatibility redirect
# ==============================================================================

def phase4_create_compat_redirect():
    """Create a compatibility module that redirects studio.* → studio.* imports.

    This is needed for external pip packages (comfy-kitchen, comfy-aimdo) that
    might import from our studio namespace.
    """
    print("\n=== PHASE 4: Create compatibility redirect ===")

    # Create studio/ as a minimal redirect package
    comfy_dir = ROOT / "studio"
    comfy_dir.mkdir(exist_ok=True)

    init_content = '''\
"""
Backward-compatibility redirect: studio.* → studio.*

This module exists ONLY to support external pip packages that import from
the old 'studio' namespace. All actual code lives in studio/.

External packages that may need this:
  - comfy-kitchen (imports studio.* internals)
  - comfy-aimdo (imports studio.* internals)
  - comfyui_manager (imports studio.* internals)
"""
import importlib
import sys
import types

# Map of old module prefixes → new module prefixes
_REDIRECTS = {
    "studio": "studio",
    "studio_extras": "studio_extras",
    "studio_api": "studio_api",
    "studio_api_nodes": "studio_api_nodes",
    "studio_config": "studio_config",
    "studio_execution": "studio_execution",
}

# External packages that should NOT be redirected
_EXTERNAL = {
    "comfy_kitchen", "comfy_aimdo", "comfyui_manager",
    "comfyui_frontend_package", "comfyui_workflow_templates",
    "comfyui_embedded_docs",
}


class _ComfyRedirectFinder:
    """Meta path finder that redirects studio* imports to studio*."""

    def find_module(self, fullname, path=None):
        # Don't redirect external packages
        top_level = fullname.split(".")[0]
        if top_level in _EXTERNAL:
            return None

        # Check if this is a studio* module that should be redirected
        for old_prefix in _REDIRECTS:
            if fullname == old_prefix or fullname.startswith(old_prefix + "."):
                return self
        return None

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]

        # Find the matching redirect
        for old_prefix, new_prefix in sorted(_REDIRECTS.items(), key=lambda x: len(x[0]), reverse=True):
            if fullname == old_prefix:
                new_name = new_prefix
                break
            elif fullname.startswith(old_prefix + "."):
                new_name = new_prefix + fullname[len(old_prefix):]
                break
        else:
            raise ImportError(f"No redirect found for {fullname}")

        # Import the studio module
        mod = importlib.import_module(new_name)
        sys.modules[fullname] = mod
        return mod


# Install the redirect finder
sys.meta_path.insert(0, _ComfyRedirectFinder())

# Make this module act like the studio package
from studio import *  # noqa: F401, F403, E402
'''

    (comfy_dir / "__init__.py").write_text(init_content, encoding='utf-8')
    print("  Created studio/__init__.py (redirect to studio)")

    # Also create studio_extras, studio_api, etc. redirect modules
    for old, new in _REDIRECTS_FOR_COMPAT.items():
        if old == "studio":
            continue  # Already handled above
        redirect_dir = ROOT / old
        if not redirect_dir.exists():
            redirect_dir.mkdir(exist_ok=True)
            init = f'''\
"""Backward-compatibility redirect: {old}.* → {new}.*"""
import sys
import importlib
try:
    _mod = importlib.import_module("{new}")
    sys.modules[__name__] = _mod
    # Copy attributes
    for _attr in dir(_mod):
        if not _attr.startswith("__"):
            globals()[_attr] = getattr(_mod, _attr)
except ImportError:
    pass
'''
            (redirect_dir / "__init__.py").write_text(init, encoding='utf-8')
            print(f"  Created {old}/__init__.py (redirect to {new})")


# Used by phase4
_REDIRECTS_FOR_COMPAT = {
    "studio": "studio",
    "studio_extras": "studio_extras",
    "studio_api": "studio_api",
    "studio_api_nodes": "studio_api_nodes",
    "studio_config": "studio_config",
    "studio_execution": "studio_execution",
}


# ==============================================================================
# PHASE 5: Update special references
# ==============================================================================

def phase5_special_fixes():
    """Fix special cases that the generic rename doesn't catch."""
    print("\n=== PHASE 5: Special fixes ===")

    # 1. studio/node_types/ → after rename becomes studio/node_types/
    #    Internal references like "from studio.node_types" → "from studio.node_types"
    #    This should already be handled by phase 2, but let's verify the README
    readme = ROOT / "studio" / "node_types" / "README.md"
    if readme.exists():
        content = readme.read_text()
        new_content = content.replace("studio.node_types", "studio.node_types")
        if new_content != content:
            readme.write_text(new_content)
            print("  Fixed studio/node_types/README.md")

    # 2. Fix the version file reference
    # studio_version → studio_version is handled by phase 2

    # 3. Fix any studio.db references (runtime DB path)
    # These should stay as-is for backward compat, but let's rename to studio.db
    for f in ROOT.rglob("*.py"):
        if '.git' in str(f) or '__pycache__' in str(f):
            continue
        try:
            content = f.read_text()
        except:
            continue
        if "studio.db" in content:
            new_content = content.replace("studio.db", "studio.db")
            if new_content != content:
                f.write_text(new_content)
                print(f"  Fixed DB reference in {f.relative_to(ROOT)}")

    # 4. Fix the log file references
    for f in ROOT.rglob("*.py"):
        if '.git' in str(f) or '__pycache__' in str(f):
            continue
        try:
            content = f.read_text()
        except:
            continue
        if "studio.log" in content:
            new_content = content.replace("studio.log", "studio.log")
            if new_content != content:
                f.write_text(new_content)
                print(f"  Fixed log reference in {f.relative_to(ROOT)}")

    # 5. Fix Dockerfile references
    dockerfile = ROOT / "Dockerfile"
    if dockerfile.exists():
        content = dockerfile.read_text()
        new_content = rename_module_in_string(content)
        if new_content != content:
            dockerfile.write_text(new_content)
            print("  Fixed Dockerfile")

    # 6. Fix blueprints JSON files - these contain node class names and display text
    blueprints_dir = ROOT / "blueprints"
    if blueprints_dir.exists():
        for bp in blueprints_dir.glob("*.json"):
            try:
                content = bp.read_text()
            except:
                continue
            # In blueprints, "ComfyUI" appears in display text and class names
            # The class names reference node types which are registered at runtime
            # We should update display text but NOT class type names (they're defined in code)
            new_content = content.replace('"ComfyUI', '"HanzoStudio')
            new_content = new_content.replace("ComfyUI", "Hanzo Studio")
            if new_content != content:
                bp.write_text(new_content)
                print(f"  Fixed blueprint: {bp.name}")

    print("  Phase 5 complete")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    print("=" * 60)
    print("Hanzo Studio Module Rename: studio* → studio*")
    print("=" * 60)

    phase1_git_mv()
    phase2_update_python_imports()
    phase3_update_other_files()
    # Skip phase 4 for now — we'll add compat shims only if needed after testing
    # phase4_create_compat_redirect()
    phase5_special_fixes()

    print("\n" + "=" * 60)
    print("RENAME COMPLETE")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Run: python -c 'import studio; print(studio.__file__)' to verify")
    print("  2. Run: python main.py --cpu to test server startup")
    print("  3. Run tests if available")
    print("  4. If external packages break, uncomment phase4 for compat shims")


if __name__ == "__main__":
    main()
