"""Tests for the identity / session API and the active-org override.

The pure org-resolution rule is checked directly; the ``/v1/session`` +
``/v1/session/org`` + ``/v1/session/project`` + ``/logout`` routes are exercised
against a real aiohttp app with a middleware that injects the IAM user the auth
layer would have set, so cookie round-trips and status codes are the real thing.
"""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from middleware import iam_auth_middleware as m
from middleware import session as s


# --- pure org resolution -------------------------------------------------

def test_resolve_org_switches_to_member_org():
    assert s.resolve_org("acme", ["acme", "globex"], "globex") == "globex"


def test_resolve_org_rejects_non_member():
    assert s.resolve_org("acme", ["acme"], "globex") == "acme"


def test_resolve_org_no_cookie_keeps_base():
    assert s.resolve_org("acme", ["acme", "globex"], None) == "acme"


# --- org list extraction from claims ------------------------------------

def test_orgs_home_first_and_deduped():
    orgs = m._orgs_from_claims({"owner": "acme", "organizations": ["acme", "globex"]})
    assert orgs == ["acme", "globex"]


def test_orgs_from_comma_string():
    assert m._orgs_from_claims({"owner": "acme", "orgs": "globex, initech"}) == ["acme", "globex", "initech"]


def test_orgs_from_group_dicts():
    orgs = m._orgs_from_claims({"organization": "acme", "groups": [{"name": "globex"}, {"name": "acme"}]})
    assert orgs == ["acme", "globex"]


def test_orgs_defaults_to_default():
    assert m._orgs_from_claims({}) == ["default"]


def test_claims_to_user_carries_orgs():
    user = m._claims_to_user({"owner": "acme", "organizations": ["globex"], "name": "A", "email": "a@x"})
    assert user["org_id"] == "acme"
    assert user["orgs"] == ["acme", "globex"]


# --- active-org override (auth-on path, copy not mutation) ---------------

def test_with_active_org_switches_and_copies():
    base = {"org_id": "acme", "orgs": ["acme", "globex"], "sub": "u"}
    got = m._with_active_org(base, "globex")
    assert got["org_id"] == "globex"
    assert base["org_id"] == "acme"  # original never mutated -> cache not poisoned


def test_with_active_org_non_member_is_noop():
    base = {"org_id": "acme", "orgs": ["acme"]}
    assert m._with_active_org(base, "globex") is base


# --- route integration ---------------------------------------------------

def _app(iam_user):
    app = web.Application()

    @web.middleware
    async def inject(request, handler):
        if iam_user is not None:
            request["iam_user"] = iam_user
        return await handler(request)

    app.middlewares.append(inject)
    routes = web.RouteTableDef()
    s.add_session_routes(routes, None)
    app.add_routes(routes)
    return app


def _run(coro):
    return asyncio.run(coro)


ANTJE = {"sub": "hanzo/antje", "name": "Antje", "email": "a@hanzo.ai",
         "org_id": "hanzo", "orgs": ["hanzo", "labs"], "avatar": "https://cdn/a.png"}


def test_session_returns_identity():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.get("/v1/session")
            assert r.status == 200
            return await r.json()
    body = _run(go())
    assert body["authenticated"] is True
    assert body["user"] == "Antje"
    assert body["email"] == "a@hanzo.ai"
    assert body["org"] == "hanzo"
    assert body["orgs"] == ["hanzo", "labs"]
    assert body["project"] == "default"
    assert body["console_url"].startswith("http")


def test_session_local_stub_when_unauthenticated():
    async def go():
        async with TestClient(TestServer(_app(None))) as c:
            r = await c.get("/v1/session")
            return r.status, await r.json()
    status, body = _run(go())
    assert status == 200
    assert body["authenticated"] is False
    assert body["user"] == "Local User"


def test_org_switch_sets_cookie_and_rescopes():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.post("/v1/session/org", json={"org": "labs"})
            assert r.status == 200
            assert (await r.json())["org"] == "labs"
            # The cookie the switch set now re-scopes the session response.
            r2 = await c.get("/v1/session")
            return (await r2.json())["org"]
    assert _run(go()) == "labs"


def test_org_switch_rejects_non_member():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.post("/v1/session/org", json={"org": "evilcorp"})
            return r.status
    assert _run(go()) == 403


def test_org_switch_requires_org():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.post("/v1/session/org", json={})
            return r.status
    assert _run(go()) == 400


def test_project_switch_roundtrips():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.post("/v1/session/project", json={"project": "fashion"})
            assert r.status == 200
            r2 = await c.get("/v1/session")
            return (await r2.json())["project"]
    assert _run(go()) == "fashion"


def test_project_switch_rejects_path_traversal():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.post("/v1/session/project", json={"project": "../etc"})
            return r.status
    assert _run(go()) == 400


def test_logout_clears_session_cookie_and_redirects():
    async def go():
        async with TestClient(TestServer(_app(ANTJE))) as c:
            r = await c.get("/logout", allow_redirects=False)
            return r.status, r.headers.get("Location", ""), r.headers
    status, location, headers = _run(go())
    assert status == 302
    assert "/login" in location
    # The auth cookie is expired (cleared) by the logout response.
    set_cookies = "; ".join(headers.getall("Set-Cookie", []))
    assert s.SESSION_COOKIE in set_cookies
