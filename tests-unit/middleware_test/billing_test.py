"""
Unit tests for the render-completion billing hook (middleware.billing_middleware).

No network, no torch, no GPU: aiohttp.ClientSession is replaced with a fake that
records every POST and lets each test dictate the response (200/4xx/5xx) or raise
a timeout. Covers the four things that matter for money-safe fire-and-forget
metering: a clean success emits exactly one correctly-shaped meter event, the
prompt_id rides through as the idempotency key, a timeout is swallowed (never
raises, never blocks) and retried once, and — the local-dev guarantee — an
unconfigured environment emits nothing at all.
"""
import asyncio

import pytest

from middleware import billing_middleware as bm


class _FakeResp:
    def __init__(self, status=201, body=""):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    """Drop-in for aiohttp.ClientSession. Captures posts; behavior is class state."""
    calls: list = []
    respond = staticmethod(lambda payload: _FakeResp(201))  # payload -> _FakeResp | raises

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        _FakeSession.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeSession.respond(json)  # may return a resp CM or raise


@pytest.fixture
def fake_http(monkeypatch):
    _FakeSession.calls = []
    _FakeSession.respond = staticmethod(lambda payload: _FakeResp(201))
    monkeypatch.setattr(bm.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("STUDIO_BILLING_URL", "http://commerce.test/v1/billing/meter-events")
    monkeypatch.setenv("STUDIO_COMMERCE_TOKEN", "tok-123")
    monkeypatch.setenv("STUDIO_BILLING_METER", "meter-render")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# count_outputs — quantity is the number of artifacts a completed render made
# ---------------------------------------------------------------------------

def test_count_outputs_sums_media_across_nodes():
    hist = {
        "outputs": {
            "9": {"images": [{"filename": "a.png"}, {"filename": "b.png"}]},
            "12": {"gifs": [{"filename": "c.webp"}]},
        }
    }
    assert bm.count_outputs(hist) == 3


def test_count_outputs_handles_empty_and_none():
    assert bm.count_outputs(None) == 0
    assert bm.count_outputs({}) == 0
    assert bm.count_outputs({"outputs": {"1": {"text": "no media here"}}}) == 0


# ---------------------------------------------------------------------------
# record_render — the fire-and-forget metered event
# ---------------------------------------------------------------------------

def test_success_emits_one_correct_event(fake_http, configured):
    _run(bm.record_render(org_id="acme", prompt_id="p-1", n_outputs=2))

    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["url"] == "http://commerce.test/v1/billing/meter-events"
    assert call["headers"]["Authorization"] == "Bearer tok-123"

    events = call["json"]["events"]
    assert len(events) == 1
    evt = events[0]
    assert evt["meterId"] == "meter-render"
    assert evt["userId"] == "acme"          # billing subject = IAM org
    assert evt["value"] == 2                 # quantity = n_outputs
    assert evt["timestamp"].endswith("Z")    # RFC3339 UTC


def test_idempotency_key_is_prompt_id(fake_http, configured):
    _run(bm.record_render(org_id="acme", prompt_id="prompt-abc", n_outputs=1))
    assert fake_http.calls[0]["json"]["events"][0]["idempotency"] == "prompt-abc"


def test_timeout_does_not_block_and_retries_once(fake_http, configured):
    def _boom(payload):
        raise asyncio.TimeoutError("simulated commerce timeout")

    fake_http.respond = staticmethod(_boom)

    # Must NOT raise — a render is never failed for billing.
    _run(bm.record_render(org_id="acme", prompt_id="p-2", n_outputs=1))

    # Fired the initial attempt + exactly one retry, then dropped.
    assert len(fake_http.calls) == 2


def test_client_error_is_not_retried(fake_http, configured):
    fake_http.respond = staticmethod(lambda payload: _FakeResp(400, "bad meter"))
    _run(bm.record_render(org_id="acme", prompt_id="p-3", n_outputs=1))
    assert len(fake_http.calls) == 1  # 4xx is deterministic — no retry


def test_server_error_is_retried_once(fake_http, configured):
    fake_http.respond = staticmethod(lambda payload: _FakeResp(503, "unavailable"))
    _run(bm.record_render(org_id="acme", prompt_id="p-4", n_outputs=1))
    assert len(fake_http.calls) == 2  # 5xx is transient — retry once, then drop


def test_unconfigured_env_is_a_noop(fake_http, monkeypatch):
    # Local dev: none of the STUDIO_BILLING_* vars set → zero behavior change.
    monkeypatch.delenv("STUDIO_BILLING_URL", raising=False)
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN", raising=False)
    monkeypatch.delenv("STUDIO_BILLING_METER", raising=False)

    _run(bm.record_render(org_id="acme", prompt_id="p-5", n_outputs=9))
    assert fake_http.calls == []  # no HTTP attempted


def test_partial_config_is_a_noop(fake_http, monkeypatch):
    # URL + token but no meter id → still a no-op (all three are required).
    monkeypatch.setenv("STUDIO_BILLING_URL", "http://commerce.test/v1/billing/meter-events")
    monkeypatch.setenv("STUDIO_COMMERCE_TOKEN", "tok-123")
    monkeypatch.delenv("STUDIO_BILLING_METER", raising=False)

    _run(bm.record_render(org_id="acme", prompt_id="p-6", n_outputs=1))
    assert fake_http.calls == []
