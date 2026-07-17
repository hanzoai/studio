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


# ── The cloud pod NEVER renders locally ──────────────────────────────────────────

class _Req:
    """Minimal stand-in for an aiohttp request that _bearer reads."""
    def __init__(self, auth="Bearer u"):
        self.headers = {"Authorization": auth} if auth else {}
        self.cookies = {}


class _Resp:
    def read(self):
        return b"{}"


def _capture_enqueue(monkeypatch):
    """Mock the gpu-jobs POST + input collection; return a dict the enqueue fills."""
    sent: dict = {}

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(g.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(g, "_collect_input_images", lambda prompt, org: [])
    monkeypatch.setattr(g, "_track_dispatched",
                        lambda org, pid, prompt, node="gpu": sent.update(node=node))
    return sent


def test_modelless_pod_enqueues_even_when_org_sees_no_gpu(monkeypatch):
    """The karma case: the org-scoped fleet read shows no GPU (workers live under a
    sibling org), but a MODEL-LESS pod must still enqueue to gpu-jobs and wait — never
    self-POST the GPU-less pod (the instant 'failed validation')."""
    monkeypatch.setattr(g, "_online_gpu_nodes", lambda tok: [])   # org-blind
    monkeypatch.setattr(g, "has_local_models", lambda: False)     # cloud coordinator
    sent = _capture_enqueue(monkeypatch)
    ok = g.dispatch_if_worker(_Req("Bearer karma"), "karma", "pid1", {})
    assert ok is True                       # enqueued to the worker lane, NOT local
    assert "gpu-jobs" in sent["url"]
    assert sent["auth"] == "Bearer karma"   # tenancy still flows from the user token
    assert sent["node"] == "gpu"            # claimer decided at pick-up


def test_modelless_pod_never_returns_local(monkeypatch):
    """Even if the enqueue POST fails, a model-less pod returns False so server.py
    yields a retryable 503 — it must NEVER fall through to local validation."""
    monkeypatch.setattr(g, "_online_gpu_nodes", lambda tok: [])
    monkeypatch.setattr(g, "has_local_models", lambda: False)
    monkeypatch.setattr(g, "_collect_input_images", lambda prompt, org: [])

    def boom(req, timeout=0):
        raise OSError("gpu-jobs unreachable")

    monkeypatch.setattr(g.urllib.request, "urlopen", boom)
    # False here means "not enqueued"; server.py then 503s on a model-less pod. The
    # point: dispatch never claims a local render happened.
    assert g.dispatch_if_worker(_Req("Bearer karma"), "karma", "pid", {}) is False


def test_box_with_models_renders_local_when_no_worker(monkeypatch):
    """A box that HAS its own models (local dev / worker node) still renders locally
    when it sees no online worker — unchanged behavior."""
    monkeypatch.setattr(g, "_online_gpu_nodes", lambda tok: [])
    monkeypatch.setattr(g, "has_local_models", lambda: True)
    touched = {"urlopen": False}
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda *a, **k: touched.update(urlopen=True) or _Resp())
    assert g.dispatch_if_worker(_Req(), "acme", "pid", {}) is False
    assert touched["urlopen"] is False       # never touched the gpu-jobs API


def test_online_worker_enqueues_with_its_label(monkeypatch):
    monkeypatch.setattr(g, "_online_gpu_nodes",
                        lambda tok: [{"name": "spark", "status": "online", "gpu": True}])
    monkeypatch.setattr(g, "has_local_models", lambda: True)  # an online worker wins
    sent = _capture_enqueue(monkeypatch)
    assert g.dispatch_if_worker(_Req(), "acme", "pid", {}) is True
    assert sent["node"] == "spark"


def test_no_token_is_not_a_local_render_claim(monkeypatch):
    assert g.dispatch_if_worker(_Req(auth=""), "acme", "pid", {}) is False


# ── The capacity gate is advisory + service-identity ─────────────────────────────

def test_service_token_prefers_worker_then_commerce(monkeypatch):
    monkeypatch.setenv("STUDIO_WORKER_TOKEN", "w")
    monkeypatch.setenv("STUDIO_COMMERCE_TOKEN", "c")
    assert g._service_token() == "Bearer w"
    monkeypatch.delenv("STUDIO_WORKER_TOKEN")
    assert g._service_token() == "Bearer c"
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN")
    assert g._service_token() == ""


def test_advisory_gate_reads_fleet_with_service_identity(monkeypatch):
    """karma's user token sees no GPU; the SERVICE token sees the fleet's worker — the
    gate reports online, so every org's UI reflects true availability."""
    seen = []

    def fleet(tok):
        seen.append(tok)
        return [{"name": "spark", "status": "online", "gpu": True}] if tok == "Bearer svc" else []

    monkeypatch.setattr(g, "_fleet_machines", fleet)
    monkeypatch.setenv("STUDIO_WORKER_TOKEN", "svc")
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN", raising=False)
    assert g.has_online_gpu_advisory("Bearer karma") is True
    assert seen[0] == "Bearer svc"           # service identity consulted FIRST


def test_advisory_gate_falls_back_to_user_token(monkeypatch):
    monkeypatch.delenv("STUDIO_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN", raising=False)
    monkeypatch.setattr(g, "_fleet_machines",
                        lambda tok: [{"name": "spark", "status": "online", "gpu": True}]
                        if tok == "Bearer user" else [])
    assert g.has_online_gpu_advisory("Bearer user") is True


def test_advisory_gate_false_when_nothing_online(monkeypatch):
    monkeypatch.delenv("STUDIO_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN", raising=False)
    monkeypatch.setattr(g, "_fleet_machines", lambda tok: [])
    assert g.has_online_gpu_advisory("Bearer user") is False
