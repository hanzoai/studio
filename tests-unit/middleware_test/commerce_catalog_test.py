"""The commerce ⇄ studio lane: read the catalog/store/listing, publish a render to the
storefront listing karma.style serves.

Real aiohttp app + a stub upstream, so the URL convention, the payload shape and the
honesty of a refusal are all asserted against the wire — not against mocks of our own
helpers. The payload MUST match cloud's own StorefrontPublish
(PUT /v1/store/:id/listing/:slug with {slug, headerImage:{type,url,alt}}) or a render
published from Studio would land differently than one published through the CMS.
"""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from middleware import commerce_catalog as cc


def _run(coro):
    return asyncio.run(coro)


def _studio(monkeypatch, upstream_base, org="acme"):
    monkeypatch.setattr(cc, "CLOUD_URL", upstream_base.rstrip("/"))
    # The stub upstream doubles as the public asset surface, so the anonymous
    # readability check the publish path runs is exercised on the real wire too.
    monkeypatch.setattr(cc, "ASSET_BASE", upstream_base.rstrip("/"))
    app = web.Application()
    routes = web.RouteTableDef()
    cc.add_commerce_routes(routes, org_of=lambda r: org)
    app.add_routes(routes)
    return app


async def _upstream(handlers):
    app = web.Application()
    routes = web.RouteTableDef()
    for (method, path), fn in handlers.items():
        routes.route(method, path)(fn)
    if not any(m == "HEAD" for (m, _) in handlers):
        # Default: the asset IS publicly readable.
        async def asset(r):
            return web.Response()
        routes.route("HEAD", "/orgs/{rest:.*}")(asset)
    app.add_routes(routes)
    server = TestServer(app)
    await server.start_server()
    return server


# --- the URL convention -------------------------------------------------------

def test_asset_url_uses_the_one_key_convention(monkeypatch):
    """Byte-identical to content_publish.object_key (orgs/<org>/output/<rel>) — the
    listing, the CMS record and the mirrored bytes must never disagree."""
    monkeypatch.setattr(cc, "ASSET_BASE", "https://assets.hanzo.ai")
    assert cc.asset_url("acme", "fixes/a_00001_.png") == \
        "https://assets.hanzo.ai/orgs/acme/output/fixes/a_00001_.png"
    assert "//orgs" not in cc.asset_url("acme", "/leading.png")


def test_default_asset_base_is_a_public_surface_never_the_credentialed_s3_api(monkeypatch):
    """s3.hanzo.ai is the CREDENTIALED S3 API: it declares one admin identity and no
    anonymous one, so every unauthenticated GET is 403 — verified in prod against a real
    render. A listing built on that base can never render, so it must not be the default.
    The public read surface is hanzoai/static in front of the same bucket, which keeps the
    key convention byte-identical (URL path == bucket key)."""
    import importlib
    monkeypatch.delenv("STUDIO_ASSET_BASE_URL", raising=False)
    importlib.reload(cc)
    try:
        assert cc.ASSET_BASE == "https://assets.hanzo.ai"
        assert "s3.hanzo.ai" not in cc.ASSET_BASE
        assert cc.asset_url("acme", "v/a.png") == "https://assets.hanzo.ai/orgs/acme/output/v/a.png"
        monkeypatch.setenv("STUDIO_ASSET_BASE_URL", "https://cdn.example/studio/")
        importlib.reload(cc)
        assert cc.ASSET_BASE == "https://cdn.example/studio"        # trailing / stripped
    finally:
        monkeypatch.delenv("STUDIO_ASSET_BASE_URL", raising=False)
        importlib.reload(cc)


# --- the URL must actually RESOLVE, anonymously -------------------------------

def test_publish_refuses_an_asset_that_is_not_publicly_readable(monkeypatch):
    """headerImage.url is PERSISTED and then read by an anonymous browser forever after.
    An unreadable one is a permanent silent breakage, so it is refused — with the observed
    status and the fix — and NO listing is written."""
    wrote = {}

    async def asset(r):
        return web.Response(status=403)

    async def store(r):
        return web.json_response({"store": {"id": "STORE123"}})

    async def listing(r):
        wrote["hit"] = True
        return web.json_response({"ok": True})

    async def go():
        up = await _upstream({("HEAD", "/orgs/{rest:.*}"): asset,
                              ("GET", "/v1/store/current"): store,
                              ("PUT", "/v1/store/{sid}/listing/{slug}"): listing})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            r = await c.post("/v1/commerce/publish", json={"path": "valentina/hero.png"})
            out = await r.json()
            await up.close()
            return r.status, out
    status, out = _run(go())
    assert status == 409
    assert "ok" not in out and "hit" not in wrote      # never written, never a false success
    assert "not publicly readable" in out["error"]
    assert "403" in out["error"] and out["asset_status"] == 403
    assert "STUDIO_ASSET_BASE_URL" in out["error"]     # …and how to fix it
    assert "s3.hanzo.ai" in out["error"]               # …and the trap that caused it


def test_publish_says_retry_when_the_mirror_has_not_landed_the_render_yet(monkeypatch):
    """The output mirror is a ~30s poll loop, so a just-finished render is briefly absent.
    That is a different problem from an unreadable surface and gets a different answer."""
    async def asset(r):
        return web.Response(status=404)

    async def store(r):
        return web.json_response({"store": {"id": "S1"}})

    async def go():
        up = await _upstream({("HEAD", "/orgs/{rest:.*}"): asset,
                              ("GET", "/v1/store/current"): store})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            r = await c.post("/v1/commerce/publish", json={"path": "v/a.png"})
            out = await r.json()
            await up.close()
            return r.status, out
    status, out = _run(go())
    assert status == 409 and out["asset_status"] == 404
    assert "publish again in a moment" in out["error"]
    assert "STUDIO_ASSET_BASE_URL" not in out["error"]   # not the same failure, not the same fix


def test_the_readability_check_carries_no_credential(monkeypatch):
    """A credentialed read proves nothing about what a customer's browser sees, so the
    check must be anonymous even when a store token is configured."""
    seen = {}

    async def asset(r):
        seen["auth"] = r.headers.get("Authorization")
        return web.Response()

    async def store(r):
        return web.json_response({"store": {"id": "S1"}})

    async def listing(r):
        return web.json_response({"ok": True})

    async def go():
        up = await _upstream({("HEAD", "/orgs/{rest:.*}"): asset,
                              ("GET", "/v1/store/current"): store,
                              ("PUT", "/v1/store/{sid}/listing/{slug}"): listing})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            r = await c.post("/v1/commerce/publish", json={"path": "v/a.png"})
            await up.close()
            return r.status
    monkeypatch.setenv("STUDIO_STORE_TOKEN", "st-live-abc")
    assert _run(go()) == 200
    assert seen["auth"] is None


def test_an_unreachable_asset_surface_is_refused_not_published(monkeypatch):
    async def store(r):
        return web.json_response({"store": {"id": "S1"}})

    async def go():
        up = await _upstream({("GET", "/v1/store/current"): store})
        base = f"http://127.0.0.1:{up.port}"
        app = _studio(monkeypatch, base)
        monkeypatch.setattr(cc, "ASSET_BASE", "http://127.0.0.1:9")   # nothing listening
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/v1/commerce/publish", json={"path": "v/a.png"})
            out = await r.json()
            await up.close()
            return r.status, out
    status, out = _run(go())
    assert status == 409 and out["asset_status"] == 0
    assert "could not be reached" in out["error"]


# --- read: catalog / store ----------------------------------------------------

def test_reads_catalog_and_store(monkeypatch):
    async def catalog(r):
        return web.json_response({"categories": [{"id": "swim"}]})

    async def store(r):
        return web.json_response({"store": {"id": "STORE123", "default_currency": "usd"}})

    async def go():
        up = await _upstream({("GET", "/v1/commerce/catalog"): catalog,
                              ("GET", "/v1/store/current"): store})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            a = await (await c.get("/v1/commerce/catalog")).json()
            b = await (await c.get("/v1/commerce/store")).json()
            await up.close()
            return a, b
    cat, st = _run(go())
    assert cat["categories"][0]["id"] == "swim"
    assert st["store"]["id"] == "STORE123"


# --- write: the storefront listing -------------------------------------------

def test_publish_writes_the_same_edge_cloud_writes(monkeypatch):
    seen = {}

    async def store(r):
        return web.json_response({"store": {"id": "STORE123"}})

    async def listing(r):
        seen["path"] = r.path
        seen["body"] = await r.json()
        return web.json_response({"slug": r.match_info["slug"], "ok": True})

    async def go():
        up = await _upstream({("GET", "/v1/store/current"): store,
                              ("PUT", "/v1/store/{sid}/listing/{slug}"): listing})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            r = await c.post("/v1/commerce/publish",
                             json={"path": "valentina/hero_00001_.png", "alt": "hero"})
            out = await r.json()
            await up.close()
            return r.status, out
    status, out = _run(go())
    assert status == 200 and out["ok"] is True
    # slug defaults to the asset's DESIGN folder — the design==slug join the CMS uses.
    assert out["slug"] == "valentina"
    assert seen["path"] == "/v1/store/STORE123/listing/valentina"
    # …and the payload is cloud's StorefrontPublish shape, exactly.
    assert seen["body"]["slug"] == "valentina"
    hi = seen["body"]["headerImage"]
    assert hi["type"] == "image" and hi["alt"] == "hero"
    assert hi["url"].endswith("/orgs/acme/output/valentina/hero_00001_.png")


def test_publish_relays_a_scope_refusal_verbatim_and_never_claims_success(monkeypatch):
    """A user bearer usually has no store scope. The upstream says so — we must relay
    that, with the remedy, and NOT report a publish that did not happen."""
    async def store(r):
        return web.json_response({"store": {"id": "STORE123"}})

    async def listing(r):
        return web.json_response(
            {"error": {"type": "api-error", "message": "Token doesn't support this scope"}},
            status=403)

    async def go():
        up = await _upstream({("GET", "/v1/store/current"): store,
                              ("PUT", "/v1/store/{sid}/listing/{slug}"): listing})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            r = await c.post("/v1/commerce/publish", json={"path": "v/a.png"})
            out = await r.json()
            await up.close()
            return r.status, out
    status, out = _run(go())
    assert status == 403
    assert "ok" not in out                                   # never a false success
    assert "Token doesn't support this scope" in out["error"]  # upstream's words, verbatim
    assert "STUDIO_STORE_TOKEN" in out["error"]                # …and how to fix it


def test_publish_requires_a_path_and_a_derivable_slug(monkeypatch):
    async def store(r):
        return web.json_response({"store": {"id": "S1"}})

    async def go():
        up = await _upstream({("GET", "/v1/store/current"): store})
        base = f"http://127.0.0.1:{up.port}"
        async with TestClient(TestServer(_studio(monkeypatch, base))) as c:
            a = await c.post("/v1/commerce/publish", json={})
            b = await c.post("/v1/commerce/publish", json={"path": "loose.png"})
            out = (a.status, (await b.json()))
            await up.close()
            return out
    st, body = _run(go())
    assert st == 400                                   # no path
    assert "slug is required" in body["error"]         # not inside a design folder


def test_store_token_is_its_own_variable_never_the_billing_one(monkeypatch):
    """One credential, one purpose. STUDIO_COMMERCE_TOKEN is the BILLING token and is a
    DIFFERENT token with different scope (verified in prod: different hashes, and the
    billing one is refused by the store). Reusing it here would repeat the overload that
    took chat down."""
    import inspect
    src = inspect.getsource(cc)
    assert "STUDIO_STORE_TOKEN" in src
    assert "os.environ.get(\"STUDIO_COMMERCE_TOKEN\")" not in src, \
        "the billing credential must never be used as the store credential"

    monkeypatch.setenv("STUDIO_STORE_TOKEN", "st-live-abc")
    monkeypatch.delenv("STUDIO_COMMERCE_TOKEN", raising=False)
    assert cc._bearer(None) == "Bearer st-live-abc"
    monkeypatch.setenv("STUDIO_STORE_TOKEN", "Bearer st-already")
    assert cc._bearer(None) == "Bearer st-already"      # already-prefixed kept as-is


def test_unreachable_commerce_is_a_legible_503(monkeypatch):
    async def go():
        # Nothing listening on this port — the transport failure must be reported.
        async with TestClient(TestServer(_studio(monkeypatch, "http://127.0.0.1:9"))) as c:
            r = await c.get("/v1/commerce/catalog")
            return r.status, await r.json()
    status, out = _run(go())
    assert status == 503 and "could not reach the commerce API" in out["error"]
