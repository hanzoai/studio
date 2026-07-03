"""
Compute configuration management for Hanzo Studio.

Per-org compute profiles stored as JSON in the user directory under
orgs/{org_id}/compute.json. Provides CRUD operations and worker registry.
"""
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import folder_paths

logger = logging.getLogger(__name__)

# GPU type to AWS instance type mapping (canonical source — imported by visor_client)
GPU_TYPE_MAP = {
    "t4": "g4dn.xlarge",
    "v100": "p3.2xlarge",
    "a100": "p4d.24xlarge",
    "h100": "p5.48xlarge",
}

VALID_GPU_TYPES = set(GPU_TYPE_MAP.keys())
VALID_PROFILES = {"cpu", "gpu-basic", "gpu-pro", "custom"}

# Per-org file locks to prevent concurrent read-modify-write corruption
_config_locks: dict[str, threading.Lock] = {}
_config_locks_mutex = threading.Lock()


def _get_config_lock(org_id: str) -> threading.Lock:
    with _config_locks_mutex:
        if org_id not in _config_locks:
            _config_locks[org_id] = threading.Lock()
        return _config_locks[org_id]


@dataclass
class WorkerInfo:
    worker_id: str
    url: str
    device: str = "cpu"            # "cpu" | "cuda"
    status: str = "ready"          # "ready" | "busy" | "offline"
    gpu_model: Optional[str] = None
    vram_gb: Optional[int] = None
    last_heartbeat: float = 0.0

    def is_alive(self, timeout: float = 90.0) -> bool:
        return (time.time() - self.last_heartbeat) < timeout


@dataclass
class ComputeProfile:
    org_id: str = "default"
    active_profile: str = "cpu"     # "cpu" | "gpu-basic" | "gpu-pro" | "custom"
    gpu_enabled: bool = False
    gpu_type: Optional[str] = None  # "t4" | "v100" | "a100" | "h100"
    workers: list = field(default_factory=list)
    auto_provision: bool = False
    max_concurrent: int = 1
    default_engine: str = "local"   # engine id renders route to (see engine_selector)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ComputeProfile":
        workers_raw = data.get("workers", [])
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "workers"}
        profile = cls(**filtered)
        profile.workers = [
            WorkerInfo(**w) if isinstance(w, dict) else w
            for w in workers_raw
        ]
        return profile


def _config_path(org_id: str) -> str:
    """Return filesystem path for an org's compute config."""
    user_dir = folder_paths.get_user_directory()
    return os.path.join(user_dir, "orgs", org_id, "compute.json")


def load_config(org_id: str) -> ComputeProfile:
    """Load compute profile for org, returning defaults if none exists."""
    path = _config_path(org_id)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            data["org_id"] = org_id
            return ComputeProfile.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load compute config for %s: %s", org_id, e)
    return ComputeProfile(org_id=org_id)


def save_config(profile: ComputeProfile) -> None:
    """Persist compute profile to disk atomically (temp file + rename)."""
    path = _config_path(profile.org_id)
    dir_ = os.path.dirname(path)
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tmp:
        json.dump(profile.to_dict(), tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, path)
    logger.info("Saved compute config for org %s", profile.org_id)


def update_config(org_id: str, updates: dict) -> ComputeProfile:
    """Merge updates into existing profile and save (thread-safe)."""
    lock = _get_config_lock(org_id)
    with lock:
        profile = load_config(org_id)

        if "active_profile" in updates:
            val = updates["active_profile"]
            if val not in VALID_PROFILES:
                raise ValueError(f"Invalid profile: {val}. Must be one of {VALID_PROFILES}")
            profile.active_profile = val

        if "gpu_enabled" in updates:
            profile.gpu_enabled = bool(updates["gpu_enabled"])

        if "gpu_type" in updates:
            val = updates["gpu_type"]
            if val is not None and val not in VALID_GPU_TYPES:
                raise ValueError(f"Invalid gpu_type: {val}. Must be one of {VALID_GPU_TYPES}")
            profile.gpu_type = val

        if "auto_provision" in updates:
            profile.auto_provision = bool(updates["auto_provision"])

        if "max_concurrent" in updates:
            mc = int(updates["max_concurrent"])
            profile.max_concurrent = max(1, min(mc, 32))

        if "default_engine" in updates:
            profile.default_engine = str(updates["default_engine"])

        # Derive gpu_enabled from profile if not explicitly set
        if "active_profile" in updates and "gpu_enabled" not in updates:
            profile.gpu_enabled = profile.active_profile in ("gpu-basic", "gpu-pro", "custom")

        # Set defaults based on profile
        if profile.active_profile == "gpu-basic" and profile.gpu_type is None:
            profile.gpu_type = "t4"
        elif profile.active_profile == "gpu-pro" and profile.gpu_type is None:
            profile.gpu_type = "a100"

        save_config(profile)
        return profile


def register_worker(org_id: str, worker_data: dict) -> WorkerInfo:
    """Register or update a worker heartbeat (thread-safe)."""
    lock = _get_config_lock(org_id)
    with lock:
        profile = load_config(org_id)

        worker_id = worker_data.get("worker_id")
        if not worker_id:
            raise ValueError("worker_id is required")

        # Find existing or create new
        existing = None
        for w in profile.workers:
            if w.worker_id == worker_id:
                existing = w
                break

        if existing:
            existing.url = worker_data.get("url", existing.url)
            existing.device = worker_data.get("device", existing.device)
            existing.status = worker_data.get("status", "ready")
            existing.gpu_model = worker_data.get("gpu_model", existing.gpu_model)
            existing.vram_gb = worker_data.get("vram_gb", existing.vram_gb)
            existing.last_heartbeat = time.time()
            worker = existing
        else:
            worker = WorkerInfo(
                worker_id=worker_id,
                url=worker_data.get("url", ""),
                device=worker_data.get("device", "cpu"),
                status=worker_data.get("status", "ready"),
                gpu_model=worker_data.get("gpu_model"),
                vram_gb=worker_data.get("vram_gb"),
                last_heartbeat=time.time(),
            )
            profile.workers.append(worker)

        # Prune dead workers
        profile.workers = [w for w in profile.workers if w.is_alive(timeout=300)]

        save_config(profile)
        return worker


def remove_worker(org_id: str, worker_id: str) -> bool:
    """Remove a worker from the registry (thread-safe)."""
    lock = _get_config_lock(org_id)
    with lock:
        profile = load_config(org_id)
        before = len(profile.workers)
        profile.workers = [w for w in profile.workers if w.worker_id != worker_id]
        if len(profile.workers) < before:
            save_config(profile)
            return True
        return False


def get_available_gpu_worker(org_id: str) -> Optional[WorkerInfo]:
    """Find a ready GPU worker for the given org."""
    profile = load_config(org_id)
    for w in profile.workers:
        if w.device == "cuda" and w.status == "ready" and w.is_alive():
            return w
    return None
