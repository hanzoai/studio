from __future__ import annotations

import os
import re
import time
import mimetypes
import logging
from pathlib import Path
from typing import Literal, List
from collections.abc import Collection

from studio.cli_args import args

supported_pt_extensions: set[str] = {'.ckpt', '.pt', '.pt2', '.bin', '.pth', '.safetensors', '.pkl', '.sft'}

folder_names_and_paths: dict[str, tuple[list[str], set[str]]] = {}

# --base-directory - Resets all default paths configured in folder_paths with a new base path
if args.base_directory:
    base_path = os.path.abspath(args.base_directory)
else:
    base_path = os.path.dirname(os.path.realpath(__file__))

models_dir = os.path.join(base_path, "models")
folder_names_and_paths["checkpoints"] = ([os.path.join(models_dir, "checkpoints")], supported_pt_extensions)
folder_names_and_paths["configs"] = ([os.path.join(models_dir, "configs")], [".yaml"])

folder_names_and_paths["loras"] = ([os.path.join(models_dir, "loras")], supported_pt_extensions)
folder_names_and_paths["vae"] = ([os.path.join(models_dir, "vae")], supported_pt_extensions)
folder_names_and_paths["text_encoders"] = ([os.path.join(models_dir, "text_encoders"), os.path.join(models_dir, "clip")], supported_pt_extensions)
folder_names_and_paths["diffusion_models"] = ([os.path.join(models_dir, "unet"), os.path.join(models_dir, "diffusion_models")], supported_pt_extensions)
folder_names_and_paths["clip_vision"] = ([os.path.join(models_dir, "clip_vision")], supported_pt_extensions)
folder_names_and_paths["style_models"] = ([os.path.join(models_dir, "style_models")], supported_pt_extensions)
folder_names_and_paths["embeddings"] = ([os.path.join(models_dir, "embeddings")], supported_pt_extensions)
folder_names_and_paths["diffusers"] = ([os.path.join(models_dir, "diffusers")], ["folder"])
folder_names_and_paths["vae_approx"] = ([os.path.join(models_dir, "vae_approx")], supported_pt_extensions)

folder_names_and_paths["controlnet"] = ([os.path.join(models_dir, "controlnet"), os.path.join(models_dir, "t2i_adapter")], supported_pt_extensions)
folder_names_and_paths["gligen"] = ([os.path.join(models_dir, "gligen")], supported_pt_extensions)

folder_names_and_paths["upscale_models"] = ([os.path.join(models_dir, "upscale_models")], supported_pt_extensions)

folder_names_and_paths["latent_upscale_models"] = ([os.path.join(models_dir, "latent_upscale_models")], supported_pt_extensions)

folder_names_and_paths["custom_nodes"] = ([os.path.join(base_path, "custom_nodes")], set())

folder_names_and_paths["hypernetworks"] = ([os.path.join(models_dir, "hypernetworks")], supported_pt_extensions)

folder_names_and_paths["photomaker"] = ([os.path.join(models_dir, "photomaker")], supported_pt_extensions)

folder_names_and_paths["classifiers"] = ([os.path.join(models_dir, "classifiers")], {""})

folder_names_and_paths["model_patches"] = ([os.path.join(models_dir, "model_patches")], supported_pt_extensions)

folder_names_and_paths["audio_encoders"] = ([os.path.join(models_dir, "audio_encoders")], supported_pt_extensions)

output_directory = os.path.join(base_path, "output")
temp_directory = os.path.join(base_path, "temp")
input_directory = os.path.join(base_path, "input")
user_directory = os.path.join(base_path, "user")

# --- Multi-tenant org-scoped storage ---
import contextvars

_org_id: str | None = args.org_id if hasattr(args, "org_id") else None
_multi_tenant: bool = getattr(args, "multi_tenant", False)

# Per-execution org, set by the prompt worker thread before a graph runs so that
# nodes writing outputs (SaveImage -> get_output_directory) land in the org's
# namespace. A ContextVar isolates the worker thread from concurrent async web
# handlers, which resolve org explicitly per-request instead. Default None means
# "no active execution org" -> fall back to the process org (_org_id) or base.
_execution_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "studio_execution_org", default=None
)


def set_execution_org(org_id: str | None) -> None:
    """Bind the org for the current execution context (worker thread)."""
    _execution_org.set(org_id)


def get_execution_org() -> str | None:
    """Return the org bound to the current execution context, if any."""
    return _execution_org.get()


def set_org_id(org_id: str | None) -> None:
    """Set the active organization for multi-tenant path scoping."""
    global _org_id
    _org_id = org_id


def get_org_id() -> str | None:
    """Get the active organization ID."""
    return _org_id


_ORG_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _safe_org_id(oid: str) -> str:
    """The single choke point where an org value becomes a filesystem path — so it
    is also the one place to reject traversal. An org id must be a plain slug; `.`,
    `..`, `/`, `\\`, or any other character is refused so a malformed/hostile claim
    can never escape the orgs/ tree (defense-in-depth; org id is IAM-derived today)."""
    if oid in (".", "..") or not _ORG_ID_RE.match(oid):
        raise ValueError(f"invalid org_id for path scoping: {oid!r}")
    return oid


def get_org_base_path(org_id: str | None = None) -> str:
    """
    Get the org-scoped base path. If multi-tenant is enabled and org_id is set,
    returns {base_path}/orgs/{org_id}. Otherwise returns base_path.
    """
    oid = org_id or _org_id
    if _multi_tenant and oid:
        org_path = os.path.join(base_path, "orgs", _safe_org_id(oid))
        os.makedirs(org_path, exist_ok=True)
        return org_path
    return base_path


def get_org_output_directory(org_id: str | None = None) -> str:
    """Get org-scoped output directory. Falls back to the base output dir."""
    ob = get_org_base_path(org_id)
    if ob != base_path:
        d = os.path.join(ob, "output")
        os.makedirs(d, exist_ok=True)
        return d
    return output_directory


def get_org_directory(org_id: str, name: str = "") -> str:
    """The org's OWN directory — the parent of its output tree — and, given a
    name, a subdirectory of it, created on demand.

    Per-org state that is not an output lives here: templates, documents, films,
    held jobs, stacks.db. Every one of them is a sibling of ``output/`` so that
    tenancy is structural, and every one of them was re-deriving that same parent
    for itself — five copies of one sentence, in four modules.

    Single tenant has no ``orgs/<org>`` to be the parent of, so the output
    directory is its own base and the per-org files sit inside it. That is the
    behaviour those five copies had and it is why this is not
    ``get_org_base_path``, which answers the repository root instead."""
    out = Path(get_org_output_directory(org_id))
    base = out.parent if out.parent.name == org_id else out
    d = base / name if name else base
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def get_org_input_directory(org_id: str | None = None) -> str:
    """Get org-scoped input directory. Falls back to the base input dir."""
    ob = get_org_base_path(org_id)
    if ob != base_path:
        d = os.path.join(ob, "input")
        os.makedirs(d, exist_ok=True)
        return d
    return input_directory


def get_org_temp_directory(org_id: str | None = None) -> str:
    """Get org-scoped temp directory. Falls back to the base temp dir."""
    ob = get_org_base_path(org_id)
    if ob != base_path:
        d = os.path.join(ob, "temp")
        os.makedirs(d, exist_ok=True)
        return d
    return temp_directory


def get_org_user_directory(org_id: str | None = None) -> str:
    """Get org-scoped user directory."""
    ob = get_org_base_path(org_id)
    if ob != base_path:
        d = os.path.join(ob, "user")
        os.makedirs(d, exist_ok=True)
        return d
    return get_user_directory()


def get_org_models_directory(org_id: str | None = None) -> str:
    """Get org-scoped models directory (org-private models)."""
    ob = get_org_base_path(org_id)
    if ob != base_path:
        d = os.path.join(ob, "models")
        os.makedirs(d, exist_ok=True)
        return d
    return models_dir

filename_list_cache: dict[str, tuple[list[str], dict[str, float], float]] = {}

class CacheHelper:
    """
    Helper class for managing file list cache data.
    """
    def __init__(self):
        self.cache: dict[str, tuple[list[str], dict[str, float], float]] = {}
        self.active = False

    def get(self, key: str, default=None) -> tuple[list[str], dict[str, float], float]:
        if not self.active:
            return default
        return self.cache.get(key, default)

    def set(self, key: str, value: tuple[list[str], dict[str, float], float]) -> None:
        if self.active:
            self.cache[key] = value

    def clear(self):
        self.cache.clear()

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.active = False
        self.clear()

cache_helper = CacheHelper()

extension_mimetypes_cache = {
    "webp" : "image",
    "fbx" : "model",
}

def map_legacy(folder_name: str) -> str:
    legacy = {"unet": "diffusion_models",
              "clip": "text_encoders"}
    return legacy.get(folder_name, folder_name)

if not os.path.exists(input_directory):
    try:
        os.makedirs(input_directory)
    except:
        logging.error("Failed to create input directory")

def set_output_directory(output_dir: str) -> None:
    global output_directory
    output_directory = output_dir

def set_temp_directory(temp_dir: str) -> None:
    global temp_directory
    temp_directory = temp_dir

def set_input_directory(input_dir: str) -> None:
    global input_directory
    input_directory = input_dir

def get_output_directory() -> str:
    # Resolve against the current execution org (worker thread) when set; in
    # single-tenant mode get_org_output_directory returns the base dir unchanged.
    return get_org_output_directory(_execution_org.get())

def get_temp_directory() -> str:
    return get_org_temp_directory(_execution_org.get())

def get_input_directory() -> str:
    return get_org_input_directory(_execution_org.get())

def get_user_directory() -> str:
    return user_directory

def set_user_directory(user_dir: str) -> None:
    global user_directory
    user_directory = user_dir


# System User Protection - Protects system directories from HTTP endpoint access
# System Users are internal-only users that cannot be accessed via HTTP endpoints.
# They use the '__' prefix convention (similar to Python's private member convention).
SYSTEM_USER_PREFIX = "__"


def get_system_user_directory(name: str = "system") -> str:
    """
    Get the path to a System User directory.

    System User directories (prefixed with '__') are only accessible via internal API,
    not through HTTP endpoints. Use this for storing system-internal data that
    should not be exposed to users.

    Args:
        name: System user name (e.g., "system", "cache"). Must be alphanumeric
              with underscores allowed, but cannot start with underscore.

    Returns:
        Absolute path to the system user directory.

    Raises:
        ValueError: If name is empty, invalid, or starts with underscore.

    Example:
        >>> get_system_user_directory("cache")
        '/path/to/user/__cache'
    """
    if not name or not isinstance(name, str):
        raise ValueError("System user name cannot be empty")
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Invalid system user name: '{name}'")
    if name.startswith("_"):
        raise ValueError("System user name should not start with underscore")
    return os.path.join(get_user_directory(), f"{SYSTEM_USER_PREFIX}{name}")


def get_public_user_directory(user_id: str, org_id: str | None = None) -> str | None:
    """
    Get the path to a Public User directory for HTTP endpoint access.

    This function provides structural security by returning None for any
    System User (prefixed with '__'). All HTTP endpoints should use this
    function instead of directly constructing user paths.

    In multi-tenant mode the user directory is rooted under the org namespace
    (user/orgs/{org_id}/user/{user_id}); otherwise it is the flat
    user/{user_id}. Passing org_id keeps the returned path a child of
    get_org_user_directory(org_id), which callers rely on for containment
    checks.

    Args:
        user_id: User identifier from HTTP request.
        org_id: Organization the user belongs to (from the IAM token), or None.

    Returns:
        Absolute path to the user directory, or None if user_id is invalid
        or refers to a System User.

    Example:
        >>> get_public_user_directory("default")
        '/path/to/user/default'
        >>> get_public_user_directory("__system")
        None
    """
    if not user_id or not isinstance(user_id, str):
        return None
    if user_id.startswith(SYSTEM_USER_PREFIX):
        return None
    return os.path.join(get_org_user_directory(org_id), user_id)


#NOTE: used in http server so don't put folders that should not be accessed remotely
def get_directory_by_type(type_name: str) -> str | None:
    if type_name == "output":
        return get_output_directory()
    if type_name == "temp":
        return get_temp_directory()
    if type_name == "input":
        return get_input_directory()
    return None

def filter_files_content_types(files: list[str], content_types: List[Literal["image", "video", "audio", "model"]]) -> list[str]:
    """
    Example:
        files = os.listdir(folder_paths.get_input_directory())
        videos = filter_files_content_types(files, ["video"])

    Note:
        - 'model' in MIME context refers to 3D models, not files containing trained weights and parameters
    """
    global extension_mimetypes_cache
    result = []
    for file in files:
        extension = file.split('.')[-1]
        if extension not in extension_mimetypes_cache:
            mime_type, _ = mimetypes.guess_type(file, strict=False)
            if not mime_type:
                continue
            content_type = mime_type.split('/')[0]
            extension_mimetypes_cache[extension] = content_type
        else:
            content_type = extension_mimetypes_cache[extension]

        if content_type in content_types:
            result.append(file)
    return result

# determine base_dir rely on annotation if name is 'filename.ext [annotation]' format
# otherwise use default_path as base_dir
def annotated_filepath(name: str) -> tuple[str, str | None]:
    if name.endswith("[output]"):
        base_dir = get_output_directory()
        name = name[:-9]
    elif name.endswith("[input]"):
        base_dir = get_input_directory()
        name = name[:-8]
    elif name.endswith("[temp]"):
        base_dir = get_temp_directory()
        name = name[:-7]
    else:
        return name, None

    return name, base_dir


def get_annotated_filepath(name: str, default_dir: str | None=None) -> str:
    name, base_dir = annotated_filepath(name)

    if base_dir is None:
        if default_dir is not None:
            base_dir = default_dir
        else:
            base_dir = get_input_directory()  # fallback path

    return os.path.join(base_dir, name)


def exists_annotated_filepath(name) -> bool:
    name, base_dir = annotated_filepath(name)

    if base_dir is None:
        base_dir = get_input_directory()  # fallback path

    filepath = os.path.join(base_dir, name)
    return os.path.exists(filepath)


def add_model_folder_path(folder_name: str, full_folder_path: str, is_default: bool = False) -> None:
    global folder_names_and_paths
    folder_name = map_legacy(folder_name)
    if folder_name in folder_names_and_paths:
        paths, _exts = folder_names_and_paths[folder_name]
        if full_folder_path in paths:
            if is_default and paths[0] != full_folder_path:
                # If the path to the folder is not the first in the list, move it to the beginning.
                paths.remove(full_folder_path)
                paths.insert(0, full_folder_path)
        else:
            if is_default:
                paths.insert(0, full_folder_path)
            else:
                paths.append(full_folder_path)
    else:
        folder_names_and_paths[folder_name] = ([full_folder_path], set())

def get_folder_paths(folder_name: str) -> list[str]:
    folder_name = map_legacy(folder_name)
    return folder_names_and_paths[folder_name][0][:]

def recursive_search(directory: str, excluded_dir_names: list[str] | None=None) -> tuple[list[str], dict[str, float]]:
    if not os.path.isdir(directory):
        return [], {}

    if excluded_dir_names is None:
        excluded_dir_names = []

    result = []
    dirs = {}

    # Attempt to add the initial directory to dirs with error handling
    try:
        dirs[directory] = os.path.getmtime(directory)
    except FileNotFoundError:
        logging.warning(f"Warning: Unable to access {directory}. Skipping this path.")

    logging.debug("recursive file list on directory {}".format(directory))
    dirpath: str
    subdirs: list[str]
    filenames: list[str]

    for dirpath, subdirs, filenames in os.walk(directory, followlinks=True, topdown=True):
        subdirs[:] = [d for d in subdirs if d not in excluded_dir_names]
        for file_name in filenames:
            try:
                relative_path = os.path.relpath(os.path.join(dirpath, file_name), directory)
                result.append(relative_path)
            except:
                logging.warning(f"Warning: Unable to access {file_name}. Skipping this file.")
                continue

        for d in subdirs:
            path: str = os.path.join(dirpath, d)
            try:
                dirs[path] = os.path.getmtime(path)
            except FileNotFoundError:
                logging.warning(f"Warning: Unable to access {path}. Skipping this path.")
                continue
    logging.debug("found {} files".format(len(result)))
    return result, dirs

def filter_files_extensions(files: Collection[str], extensions: Collection[str]) -> list[str]:
    return sorted(list(filter(lambda a: os.path.splitext(a)[-1].lower() in extensions or len(extensions) == 0, files)))



def get_full_path(folder_name: str, filename: str) -> str | None:
    """
    Get the full path of a file in a folder, has to be a file
    """
    global folder_names_and_paths
    folder_name = map_legacy(folder_name)
    if folder_name not in folder_names_and_paths:
        return None
    folders = folder_names_and_paths[folder_name]
    filename = os.path.relpath(os.path.join("/", filename), "/")
    for x in folders[0]:
        full_path = os.path.join(x, filename)
        if os.path.isfile(full_path):
            return full_path
        elif os.path.islink(full_path):
            logging.warning("WARNING path {} exists but doesn't link anywhere, skipping.".format(full_path))

    return None


def get_full_path_or_raise(folder_name: str, filename: str) -> str:
    """
    Get the full path of a file in a folder, has to be a file
    """
    full_path = get_full_path(folder_name, filename)
    if full_path is None:
        raise FileNotFoundError(f"Model in folder '{folder_name}' with filename '{filename}' not found.")
    return full_path


def get_filename_list_(folder_name: str) -> tuple[list[str], dict[str, float], float]:
    folder_name = map_legacy(folder_name)
    global folder_names_and_paths
    output_list = set()
    folders = folder_names_and_paths[folder_name]
    output_folders = {}
    for x in folders[0]:
        files, folders_all = recursive_search(x, excluded_dir_names=[".git"])
        output_list.update(filter_files_extensions(files, folders[1]))
        output_folders = {**output_folders, **folders_all}

    return sorted(list(output_list)), output_folders, time.perf_counter()

def cached_filename_list_(folder_name: str) -> tuple[list[str], dict[str, float], float] | None:
    strong_cache = cache_helper.get(folder_name)
    if strong_cache is not None:
        return strong_cache

    global filename_list_cache
    global folder_names_and_paths
    folder_name = map_legacy(folder_name)
    if folder_name not in filename_list_cache:
        return None
    out = filename_list_cache[folder_name]

    for x in out[1]:
        time_modified = out[1][x]
        folder = x
        if os.path.getmtime(folder) != time_modified:
            return None

    folders = folder_names_and_paths[folder_name]
    for x in folders[0]:
        if os.path.isdir(x):
            if x not in out[1]:
                return None

    return out

def get_filename_list(folder_name: str) -> list[str]:
    folder_name = map_legacy(folder_name)
    out = cached_filename_list_(folder_name)
    if out is None:
        out = get_filename_list_(folder_name)
        global filename_list_cache
        filename_list_cache[folder_name] = out
    cache_helper.set(folder_name, out)
    return list(out[0])

def get_save_image_path(filename_prefix: str, output_dir: str, image_width=0, image_height=0) -> tuple[str, str, int, str, str]:
    def map_filename(filename: str) -> tuple[int, str]:
        prefix_len = len(os.path.basename(filename_prefix))
        prefix = filename[:prefix_len + 1]
        try:
            digits = int(filename[prefix_len + 1:].split('_')[0])
        except:
            digits = 0
        return digits, prefix

    def compute_vars(input: str, image_width: int, image_height: int) -> str:
        input = input.replace("%width%", str(image_width))
        input = input.replace("%height%", str(image_height))
        now = time.localtime()
        input = input.replace("%year%", str(now.tm_year))
        input = input.replace("%month%", str(now.tm_mon).zfill(2))
        input = input.replace("%day%", str(now.tm_mday).zfill(2))
        input = input.replace("%hour%", str(now.tm_hour).zfill(2))
        input = input.replace("%minute%", str(now.tm_min).zfill(2))
        input = input.replace("%second%", str(now.tm_sec).zfill(2))
        return input

    if "%" in filename_prefix:
        filename_prefix = compute_vars(filename_prefix, image_width, image_height)

    subfolder = os.path.dirname(os.path.normpath(filename_prefix))
    filename = os.path.basename(os.path.normpath(filename_prefix))

    full_output_folder = os.path.join(output_dir, subfolder)

    if os.path.commonpath((output_dir, os.path.abspath(full_output_folder))) != output_dir:
        err = "**** ERROR: Saving image outside the output folder is not allowed." + \
              "\n full_output_folder: " + os.path.abspath(full_output_folder) + \
              "\n         output_dir: " + output_dir + \
              "\n         commonpath: " + os.path.commonpath((output_dir, os.path.abspath(full_output_folder)))
        logging.error(err)
        raise Exception(err)

    try:
        counter = max(filter(lambda a: os.path.normcase(a[1][:-1]) == os.path.normcase(filename) and a[1][-1] == "_", map(map_filename, os.listdir(full_output_folder))))[0] + 1
    except ValueError:
        counter = 1
    except FileNotFoundError:
        os.makedirs(full_output_folder, exist_ok=True)
        counter = 1
    return full_output_folder, filename, counter, subfolder, filename_prefix

def get_input_subfolders() -> list[str]:
    """Returns a list of all subfolder paths in the input directory, recursively.

    Returns:
        List of folder paths relative to the input directory, excluding the root directory
    """
    input_dir = get_input_directory()
    folders = []

    try:
        if not os.path.exists(input_dir):
            return []

        for root, dirs, _ in os.walk(input_dir):
            rel_path = os.path.relpath(root, input_dir)
            if rel_path != ".":  # Only include non-root directories
                # Normalize path separators to forward slashes
                folders.append(rel_path.replace(os.sep, '/'))

        return sorted(folders)
    except FileNotFoundError:
        return []
