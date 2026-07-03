"""
Standalone tests for the engine selector (/v1/engines backing logic).
Thin wrapper over the per-org compute registry — no GPU, no torch, no network.
"""
import pytest

import folder_paths
from middleware import compute_config, engine_selector


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_paths, "get_user_directory", lambda: str(tmp_path))
    return "acme"


def test_local_always_present(org):
    out = engine_selector.list_engines(org)
    ids = [e["id"] for e in out["engines"]]
    assert ids == ["local"]
    assert out["default"] == "local"


def test_registered_worker_listed(org):
    compute_config.register_worker(org, {
        "worker_id": "gb10-1", "url": "http://gb10:8188",
        "device": "cuda", "status": "ready", "gpu_model": "GB10", "vram_gb": 96,
    })
    out = engine_selector.list_engines(org)
    ids = {e["id"] for e in out["engines"]}
    assert ids == {"local", "gb10-1"}
    gb10 = next(e for e in out["engines"] if e["id"] == "gb10-1")
    assert gb10["type"] == "worker" and gb10["status"] == "ready" and gb10["device"] == "cuda"


def test_set_and_resolve_default(org):
    compute_config.register_worker(org, {
        "worker_id": "gb10-1", "url": "http://gb10:8188", "device": "cuda", "status": "ready",
    })
    compute_config.update_config(org, {"default_engine": "gb10-1"})
    assert engine_selector.list_engines(org)["default"] == "gb10-1"
    w = engine_selector.resolve_engine_worker(org)
    assert w is not None and w.worker_id == "gb10-1"


def test_default_local_resolves_none(org):
    assert engine_selector.resolve_engine_worker(org) is None


def test_stale_default_falls_back_to_local(org):
    # Default names a worker that never registered → list reports local, no 500.
    compute_config.update_config(org, {"default_engine": "ghost"})
    assert engine_selector.list_engines(org)["default"] == "local"
    assert engine_selector.resolve_engine_worker(org) is None
