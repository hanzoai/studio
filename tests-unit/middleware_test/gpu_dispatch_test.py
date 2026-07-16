"""Unit tests for BYO-GPU dispatch node identity (middleware/gpu_dispatch.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import gpu_dispatch as g


def test_node_label_prefers_human_name():
    assert g._node_label({"name": "spark", "hostname": "h", "gpu": True}) == "spark"
    assert g._node_label({"hostname": "dbc-01", "gpu": True}) == "dbc-01"
    assert g._node_label({"worker_id": "gpu-acme-123"}) == "gpu-acme-123"
    assert g._node_label({"gpu_model": "RTX 4090"}) == "RTX 4090"
    assert g._node_label({}) == "gpu"                      # never blank


def test_online_gpu_nodes_filters_offline_and_non_gpu(monkeypatch):
    monkeypatch.setattr(g, "_fleet_machines", lambda tok: [
        {"name": "spark", "status": "online", "gpu": True},
        {"name": "idle", "status": "offline", "gpu": True},
        {"name": "cpu-box", "status": "online", "gpu": False},
    ])
    assert [m["name"] for m in g._online_gpu_nodes("tok")] == ["spark"]
    assert g._has_online_gpu("tok") is True


def test_no_online_gpu_when_all_offline(monkeypatch):
    monkeypatch.setattr(g, "_fleet_machines", lambda tok: [
        {"name": "spark", "status": "offline", "gpu": True},
    ])
    assert g._online_gpu_nodes("tok") == []
    assert g._has_online_gpu("tok") is False
