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
    app.add_routes(routes)
    server = TestServer(app)
    await server.start_server()
    return server


# --- the URL convention -------------------------------------------------------

def test_asset_url_uses_the_one_key_convention(monkeypatch):
    """Byte-identical to content_publish.object_key (orgs/<org>/output/<rel>) — the
    listing, the CMS record and the mirrored bytes must never disagree."""
    monkeypatch.setattr(cc, "ASSET_BASE", "https://s3.hanzo.ai/hanzo-studio")
    assert cc.asset_url("acme", "fixes/a_00001_.png") == \
        "https://s3.hanzo.ai/hanzo-studio/orgs/acme/output/fixes/a_00001_.png"
    assert "//orgs" not in cc.asset_url("acme", "/leading.png")


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
