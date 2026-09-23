"""
The pre-prompt balance gate (middleware.billing_middleware) and the commerce client it
reads through (middleware.commerce).

One real aiohttp stub stands in for cloud, serving both IAM's token endpoint and the
balance read, as the in-cluster cloud does. It records every request, so the wire is
asserted directly: the client_credentials mint (Basic id:secret, form body), the token
cache and its 60s margin, the balance read (path, bearer, X-Org-Id), the account echo
that proves the token acts for the org, and the gate's fail-open answer on everything
that is not a balance read for the org.
"""
import asyncio
import base64
import inspect
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from middleware import billing_middleware as bm
from middleware import commerce
from middleware import gpu_dispatch
from studio.cli_args import args


class _Cloud:
    """Stub cloud: POST /v1/iam/oauth/token + GET /v1/billing/balance, both recorded."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.mints = []
        self.reads = []
        self.token = lambda n: web.json_response({"access_token": f"tok-{n}", "expires_in": 3600})
        # Default: a token that acts for whatever org was selected.
        self.balance = lambda r: web.json_response(
            {"balance": 900, "holds": 400, "available": 500, "account": r.headers["X-Org-Id"]})

    async def _mint(self, r):
        form = await r.post()
        self.mints.append({"auth": r.headers.get("Authorization"),
                           "ctype": r.content_type, "form": dict(form)})
        return self.token(len(self.mints))

    async def _read(self, r):
        self.reads.append({"path": r.path, "query": dict(r.query),
                           "auth": r.headers.get("Authorization"),
                           "org": r.headers.get("X-Org-Id")})
        return self.balance(r)

    def run(self, fn):
        async def go():
            app = web.Application()
            app.router.add_post("/v1/iam/oauth/token", self._mint)
            app.router.add_get("/v1/billing/balance", self._read)
            server = TestServer(app)
            await server.start_server()
            base = f"http://127.0.0.1:{server.port}"
            self.monkeypatch.setattr(args, "iam_url", base)  # the IAM base auth uses
            self.monkeypatch.setenv("STUDIO_COMMERCE_URL", base)
            try:
                return await fn()
            finally:
                await server.close()
        return asyncio.run(go())


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("STUDIO_IAM_CLIENT_ID", "hanzo-studio")
    monkeypatch.setenv("STUDIO_IAM_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(commerce, "_token", {"value": "", "until": 0.0})
    monkeypatch.setattr(commerce, "_lock", asyncio.Lock())
    return _Cloud(monkeypatch)


# --- the application token ------------------------------------------------------

def test_token_is_minted_by_client_credentials_as_the_studio_iam_app(cloud):
    cloud.run(lambda: commerce.balance("acme"))
    assert len(cloud.mints) == 1
    mint = cloud.mints[0]
    assert mint["auth"] == "Basic " + base64.b64encode(b"hanzo-studio:s3cret").decode()
    assert mint["ctype"] == "application/x-www-form-urlencoded"
    assert mint["form"] == {"grant_type": "client_credentials"}  # no secret in the body


def test_token_is_cached_and_replaced_60s_before_expiry(cloud, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(commerce, "_clock", lambda: now[0])

    async def go():
        await commerce.balance("acme")
        await commerce.balance("acme")
        now[0] = 1000.0 + 3600 - 61          # 61s left: still the cached token
        await commerce.balance("acme")
        now[0] = 1000.0 + 3600 - 60          # 60s left: replaced
        await commerce.balance("acme")
    cloud.run(go)
    assert len(cloud.mints) == 2
    assert [r["auth"] for r in cloud.reads] == ["Bearer tok-1"] * 3 + ["Bearer tok-2"]


def test_token_without_a_lifetime_is_never_cached(cloud):
    cloud.token = lambda n: web.json_response({"access_token": f"tok-{n}"})

    async def go():
        await commerce.balance("acme")
        await commerce.balance("acme")
    cloud.run(go)
    assert len(cloud.mints) == 2


def test_rejected_token_is_dropped_and_minted_again(cloud):
    cloud.balance = lambda r: web.json_response({"error": "invalid token"}, status=401)

    async def go():
        for _ in range(2):
            with pytest.raises(commerce.Error):
                await commerce.balance("acme")
    cloud.run(go)
    assert len(cloud.mints) == 2


def test_iam_refusal_is_an_error_and_nothing_is_read(cloud):
    cloud.token = lambda n: web.json_response({"error": "invalid_client"}, status=401)

    async def go():
        with pytest.raises(commerce.Error, match="IAM token 401"):
            await commerce.balance("acme")
    cloud.run(go)
    assert cloud.reads == []


def test_no_secret_is_no_identity_and_no_request(cloud, monkeypatch):
    monkeypatch.delenv("STUDIO_IAM_CLIENT_SECRET")

    async def go():
        with pytest.raises(commerce.Error, match="STUDIO_IAM_CLIENT_SECRET"):
            await commerce.balance("acme")
    cloud.run(go)
    assert cloud.mints == [] and cloud.reads == []


# --- the balance read -------------------------------------------------------------

def test_balance_read_shape(cloud):
    body = cloud.run(lambda: commerce.balance("acme"))
    assert cloud.reads == [{"path": "/v1/billing/balance", "query": {},
                            "auth": "Bearer tok-1", "org": "acme"}]
    assert body == {"balance": 900, "holds": 400, "available": 500, "account": "acme"}


def test_account_under_the_org_is_the_org(cloud):
    cloud.balance = lambda r: web.json_response({"available": 7, "account": "acme/usd"})
    assert cloud.run(lambda: commerce.balance("acme"))["available"] == 7


@pytest.mark.parametrize("account", ["hanzo", "acmex", "acmex/usd", "", None])
def test_account_for_another_org_is_a_mismatch(cloud, account):
    """The selection fell back to the app's own org (or anything not under acme):
    the token does not act for acme, so that balance is never returned."""
    cloud.balance = lambda r: web.json_response({"available": 10_000, "account": account})

    async def go():
        with pytest.raises(commerce.Mismatch):
            await commerce.balance("acme")
    cloud.run(go)


# --- the gate ---------------------------------------------------------------------

def test_gate_allows_an_org_with_funds(cloud):
    assert cloud.run(lambda: bm.check_balance("acme")) == (True, 500)


def test_gate_refuses_an_org_with_nothing_available(cloud):
    cloud.balance = lambda r: web.json_response({"available": 0, "account": "acme"})
    assert cloud.run(lambda: bm.check_balance("acme")) == (False, 0)


def test_gate_fails_open_when_the_token_does_not_act_for_the_org(cloud, caplog):
    """Cloud answered with the app's OWN balance. That number is never used for acme —
    neither to allow nor to refuse — the balance is unread and the gate's posture
    (fail open) applies, loudly."""
    cloud.balance = lambda r: web.json_response({"available": 0, "account": "hanzo"})
    with caplog.at_level(logging.WARNING):
        assert cloud.run(lambda: bm.check_balance("acme")) == (True, -1)
    assert "token does not act for org acme" in caplog.text


def test_gate_fails_open_when_cloud_errors(cloud, caplog):
    cloud.balance = lambda r: web.json_response({"error": "ledger unavailable"}, status=503)
    with caplog.at_level(logging.WARNING):
        assert cloud.run(lambda: bm.check_balance("acme")) == (True, -1)
    assert "balance unread for org acme" in caplog.text


def test_gate_fails_open_when_cloud_is_unreachable(cloud, monkeypatch):
    async def go():
        monkeypatch.setenv("STUDIO_COMMERCE_URL", "http://127.0.0.1:1")
        return await bm.check_balance("acme")
    assert cloud.run(go) == (True, -1)


def test_gate_fails_open_without_a_service_identity(cloud, monkeypatch):
    monkeypatch.delenv("STUDIO_IAM_CLIENT_SECRET")
    assert cloud.run(lambda: bm.check_balance("acme")) == (True, -1)
    assert cloud.mints == [] and cloud.reads == []


def test_no_shared_commerce_token_is_read():
    """Billing speaks as the service's own IAM app; the shared commerce token is gone."""
    for mod in (bm, commerce, gpu_dispatch):
        assert "COMMERCE_TOKEN" not in inspect.getsource(mod), mod.__name__
