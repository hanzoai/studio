"""
studio_compat -- import-compat shim for upstream ComfyUI custom nodes.

Hanzo Studio renamed ComfyUI's internal packages (``comfy`` -> ``studio``,
``comfy_extras`` -> ``studio_extras``, ... and ``comfy/comfy_types`` ->
``studio/node_types``; see ``scripts/rename_modules.py``). Third-party custom
node packs import the upstream ``comfy*`` names, so they fail to import against
this fork.

This module installs ONE meta-path finder that lazily aliases every ``comfy*``
import to its ``studio*`` counterpart, so vendored packs run unmodified -- no
forking, no source edits, one place, one way. It is the compatibility redirect
that ``scripts/rename_modules.py`` deferred (its phase 4), implemented with the
modern ``find_spec`` API (``find_module``/``load_module`` were removed in
Python 3.12).

Activated once, from the top of ``main.py``::

    import studio_compat  # noqa: F401
"""
import importlib
import importlib.abc
import importlib.machinery
import sys

# Root package aliases: upstream ComfyUI name -> Hanzo Studio name.
_ROOTS = {
    "comfy": "studio",
    "comfy_extras": "studio_extras",
    "comfy_api": "studio_api",
    "comfy_api_nodes": "studio_api_nodes",
    "comfy_config": "studio_config",
    "comfy_execution": "studio_execution",
}

# Submodule renames applied to the target name after the root swap.
# ComfyUI's ``comfy/comfy_types`` lives at ``studio/node_types`` in this fork.
_SUBMODULE_RENAMES = {
    "studio.comfy_types": "studio.node_types",
}

# External pip packages that share the ``comfy*``/``comfyui*`` prefix but are
# NOT part of the renamed core -- never alias these (they resolve normally).
# Mirrors the EXTERNAL_PACKAGES set in scripts/rename_modules.py.
_EXTERNAL = frozenset({
    "comfy_kitchen",
    "comfy_aimdo",
    "comfyui_manager",
    "comfyui_frontend_package",
    "comfyui_workflow_templates",
    "comfyui_embedded_docs",
})


def _target_name(fullname):
    """Return the ``studio*`` module name that ``fullname`` aliases, or None."""
    root = fullname.split(".", 1)[0]
    if root in _EXTERNAL or root not in _ROOTS:
        return None
    target = _ROOTS[root] + fullname[len(root):]
    for src, dst in _SUBMODULE_RENAMES.items():
        if target == src or target.startswith(src + "."):
            target = dst + target[len(src):]
            break
    return target


# ---------------------------------------------------------------------------
# Class-name compatibility.
#
# The fork mechanically swapped the ``Comfy`` prefix for ``Studio`` on the
# public node-API classes (comfy_api.latest.ComfyExtension -> StudioExtension;
# comfy_api.latest.io.ComfyNode -> studio_api.latest.io.StudioNode, ...).
# Newer packs subclass ``io.ComfyNode`` / return ``ComfyExtension``. We apply
# the inverse swap as attribute aliases, lazily, the first time a pack imports
# the aliased module -- by which point studio_api.latest is safely importable.
# ---------------------------------------------------------------------------

def _mirror_comfy_prefix(module):
    """Bind ``Comfy<X>`` aliases for every public ``Studio<X>`` in ``module``."""
    for name in list(vars(module)):
        if name.startswith("Studio"):
            alias = "Comfy" + name[len("Studio"):]
            if not hasattr(module, alias):
                setattr(module, alias, getattr(module, name))


def _mirror_studio_api_latest(module):
    _mirror_comfy_prefix(module)
    io_mod = getattr(module, "io", None)  # studio_api.latest._io_public
    if io_mod is not None:
        _mirror_comfy_prefix(io_mod)


# target module name -> hook(module) run once, right after it is aliased.
_POST_IMPORT = {
    "studio_api.latest": _mirror_studio_api_latest,
}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target):
        self._target = target

    def create_module(self, spec):
        # Import (or reuse) the real module and hand it back as the alias.
        # Returning an already-initialized module is why exec_module is a no-op;
        # _init_module_attrs(override=False) preserves the target's __name__ etc.
        module = importlib.import_module(self._target)
        hook = _POST_IMPORT.get(self._target)
        if hook is not None:
            hook(module)
        return module

    def exec_module(self, module):
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        name = _target_name(fullname)
        if name is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(name))


def install():
    """Insert the alias finder at the head of ``sys.meta_path`` (idempotent)."""
    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())


install()
