"""worker-mode /prompt gate + the /v1/worker/execute claim seam.

Covers:
- verify_worker_token / reject_untrusted_worker_submit — the ONE policy gate that makes
  a direct /prompt POST on a worker box refuse un-tokened submits (403), so no render
  reaches a GPU without appearing in the org's gpu-jobs queue.
- POST /v1/worker/execute — validates X-Worker-Token and RUNS the claimed graph on the
  box's one render slot, returning {prompt_id}, scoped to the job's org.
"""
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# CPU CI runners have no NVIDIA driver: force studio's CPU mode BEFORE importing
# worker_client (which pulls execution -> studio.model_management, whose import-time
# device probe would otherwise call torch.cuda and crash on a GPU-less box). Same
# switch the studio pod boots with (--cpu); harmless on a GPU box.
import studio.cli_args
studio.cli_args.args.cpu = True

from middleware import worker_client as wc


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


# ── The gate policy: worker-mode /prompt refuses un-tokened submits ───────────────

def test_empty_token_dev_bypass_ONLY_outside_worker_mode(monkeypatch):
    """FAIL CLOSED: an empty/unset STUDIO_WORKER_TOKEN is a valid single-trust-domain dev
    signal ONLY when NOT in worker mode. On a worker box (worker_mode=True) an empty token
    must NOT open /prompt or /v1/worker/execute — that was the fail-open hole (KMS /v1
    migration has emptied the secret before)."""
    monkeypatch.setattr(wc, "WORKER_TOKEN", "")
    monkeypatch.setattr(wc.args, "worker_mode", False)
    assert wc.verify_worker_token(_Req()) is True            # local dev, single trust domain
    monkeypatch.setattr(wc.args, "worker_mode", True)
    assert wc.verify_worker_token(_Req()) is False           # worker box + empty token → refuse


def test_verify_worker_token_enforced(monkeypatch):
    monkeypatch.setattr(wc, "WORKER_TOKEN", "s3cret")
    assert wc.verify_worker_token(_Req({"X-Worker-Token": "s3cret"})) is True
    assert wc.verify_worker_token(_Req({"X-Worker-Token": "nope"})) is False
    assert wc.verify_worker_token(_Req()) is False
    # constant-time path (hmac.compare_digest) accepts only the exact secret
    assert wc.verify_worker_token(_Req({"X-Worker-Token": "s3cret "})) is False


def test_gate_allows_when_not_worker_mode(monkeypatch):
    monkeypatch.setattr(wc, "WORKER_TOKEN", "s3cret")
    assert wc.reject_untrusted_worker_submit(_Req(), worker_mode=False) is None


def test_gate_403_in_worker_mode_without_token(monkeypatch):
    monkeypatch.setattr(wc, "WORKER_TOKEN", "s3cret")
    resp = wc.reject_untrusted_worker_submit(_Req(), worker_mode=True)
    assert resp is not None and resp.status == 403           # direct /prompt refused


def test_gate_403_in_worker_mode_with_EMPTY_token(monkeypatch):
    """Fail closed even when the secret is empty (KMS-emptied): worker mode + empty token
    still refuses a direct /prompt (403) — runtime belt to main.py's refuse-to-start guard."""
    monkeypatch.setattr(wc, "WORKER_TOKEN", "")
    monkeypatch.setattr(wc.args, "worker_mode", True)
    resp = wc.reject_untrusted_worker_submit(_Req(), worker_mode=True)
    assert resp is not None and resp.status == 403


def test_gate_allows_worker_mode_with_token(monkeypatch):
    monkeypatch.setattr(wc, "WORKER_TOKEN", "s3cret")
    ok = _Req({"X-Worker-Token": "s3cret"})
    assert wc.reject_untrusted_worker_submit(ok, worker_mode=True) is None


# ── The claim seam: POST /v1/worker/execute validates token + runs locally ────────

class _Renderer:
    """One slot: it takes the first item and refuses anything on top of it."""

    def __init__(self):
        self.items = []

    def start(self, item):
        if self.items:
            return False
        self.items.append(item)
        return True


class _NRM:
    def apply_replacements(self, prompt):
        pass


class _Server:
    def __init__(self):
        self.renderer = _Renderer()
        self.node_replace_manager = _NRM()


@pytest.fixture
def worker_app(monkeypatch):
    import execution

    async def fake_validate(pid, prompt, targets=None):
        return (True, None, list(prompt.keys()), {})

    monkeypatch.setattr(execution, "validate_prompt", fake_validate)
    monkeypatch.setattr(wc, "WORKER_TOKEN", "s3cret")
    server = _Server()
    app = web.Application()
    routes = web.RouteTableDef()
    wc.add_worker_routes(routes, server)
    app.add_routes(routes)
    app["srv"] = server
    return app


@pytest.mark.asyncio
async def test_worker_execute_rejects_without_token(worker_app, aiohttp_client):
    client = await aiohttp_client(worker_app)
    r = await client.post("/v1/worker/execute",
                          json={"prompt": {"9": {"class_type": "SaveImage", "inputs": {}}}})
    assert r.status == 401
    assert worker_app["srv"].renderer.items == []            # nothing ran


@pytest.mark.asyncio
async def test_worker_execute_runs_claimed_job_with_token(worker_app, aiohttp_client):
    client = await aiohttp_client(worker_app)
    graph = {"9": {"class_type": "SaveImage", "inputs": {}}}
    r = await client.post("/v1/worker/execute",
                          headers={"X-Worker-Token": "s3cret"},
                          json={"prompt": graph, "prompt_id": "pid-1", "org": "acme"})
    assert r.status == 200
    body = await r.json()
    assert body["prompt_id"] == "pid-1"
    items = worker_app["srv"].renderer.items
    assert len(items) == 1                                    # rendering here, now
    pid, _prompt, extra, _outputs, _sensitive = items[0]
    assert pid == "pid-1"
    assert extra["org_id"] == "acme"                          # worker-LOCAL scope label (gallery org re-derived from IAM)


@pytest.mark.asyncio
async def test_worker_execute_rejects_non_dict_prompt(worker_app, aiohttp_client):
    """A non-object prompt (e.g. an injected string) is a clean 400, not a 500."""
    client = await aiohttp_client(worker_app)
    r = await client.post("/v1/worker/execute", headers={"X-Worker-Token": "s3cret"},
                          json={"prompt": "<img src=x onerror=alert(1)>"})
    assert r.status == 400
    assert worker_app["srv"].renderer.items == []             # nothing ran


@pytest.mark.asyncio
async def test_a_second_claim_is_refused_while_the_slot_is_full(worker_app, aiohttp_client):
    """A worker holds one render and no line behind it. A second hand-in is refused so
    the job stays in the gpu-jobs lane for a worker that can actually take it."""
    client = await aiohttp_client(worker_app)
    graph = {"9": {"class_type": "SaveImage", "inputs": {}}}
    first = await client.post("/v1/worker/execute", headers={"X-Worker-Token": "s3cret"},
                              json={"prompt": graph, "prompt_id": "pid-1"})
    assert first.status == 200
    second = await client.post("/v1/worker/execute", headers={"X-Worker-Token": "s3cret"},
                               json={"prompt": graph, "prompt_id": "pid-2"})
    assert second.status == 503
    assert [it[0] for it in worker_app["srv"].renderer.items] == ["pid-1"]
