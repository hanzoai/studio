"""Tests for the IAM auth middleware's pure verification + session helpers.

The JWT path is exercised end to end with a locally generated RSA keypair:
sign an access token like Hanzo IAM would, then verify it through the same
code the middleware uses. Covers signature, exp, iss, aud and the org claim.
"""
import asyncio
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from cryptography.hazmat.primitives.asymmetric import rsa

from middleware import iam_auth_middleware as m

ISS = "https://hanzo.id"
AUD = "hanzo-studio"


@pytest.fixture(scope="module")
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _token(priv, **overrides):
    now = int(time.time())
    claims = {
        "sub": "hanzo/alice",
        "iss": ISS,
        "aud": AUD,
        "exp": now + 300,
        "iat": now,
        "owner": "acme",
        "name": "Alice",
        "email": "alice@acme.com",
        "picture": "https://cdn/x.png",
    }
    claims.update(overrides)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "test"})


def test_valid_token_extracts_org(keys):
    priv, pub = keys
    user = m.verify_jwt(_token(priv), pub, ISS, AUD)
    assert user is not None
    assert user["org_id"] == "acme"
    assert user["sub"] == "hanzo/alice"
    assert user["name"] == "Alice"
    assert user["email"] == "alice@acme.com"
    assert user["avatar"] == "https://cdn/x.png"


def test_organization_claim_fallback(keys):
    priv, pub = keys
    tok = _token(priv, owner=None, organization="globex")
    assert m.verify_jwt(tok, pub, ISS, AUD)["org_id"] == "globex"


def test_org_defaults_when_absent(keys):
    priv, pub = keys
    tok = _token(priv, owner=None, organization=None)
    assert m.verify_jwt(tok, pub, ISS, AUD)["org_id"] == "default"


def test_preferred_username_fallback(keys):
    priv, pub = keys
    tok = _token(priv, name=None, preferred_username="alice")
    assert m.verify_jwt(tok, pub, ISS, AUD)["name"] == "alice"


def test_wrong_audience_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, aud="someone-else"), pub, ISS, AUD) is None


def test_wrong_issuer_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, iss="https://evil.example"), pub, ISS, AUD) is None


def test_expired_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, exp=int(time.time()) - 10), pub, ISS, AUD) is None


def test_missing_exp_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, exp=None), pub, ISS, AUD) is None


def test_bad_signature_rejected(keys):
    priv, _pub = keys
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    assert m.verify_jwt(_token(priv), other, ISS, AUD) is None


def test_aud_array_form_accepted(keys):
    """golang-jwt serializes a single audience as a string but multi as array;
    accept both."""
    priv, pub = keys
    assert m.verify_jwt(_token(priv, aud=[AUD, "other"]), pub, ISS, AUD)["org_id"] == "acme"


# --- signed OAuth state (CSRF) ---

def test_state_roundtrip():
    payload = {"n": "nonce", "v": "verifier", "t": "/workflows", "exp": int(time.time()) + 60}
    blob = m._sign_state(payload, "secret")
    got = m._verify_state(blob, "secret")
    assert got["n"] == "nonce" and got["t"] == "/workflows"


def test_state_tamper_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) + 60}, "secret")
    body, mac = blob.split(".", 1)
    assert m._verify_state(f"{body}x.{mac}", "secret") is None


def test_state_wrong_secret_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) + 60}, "secret")
    assert m._verify_state(blob, "other") is None


def test_state_expired_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) - 1}, "secret")
    assert m._verify_state(blob, "secret") is None


# --- path exemptions ---

@pytest.mark.parametrize("path", [
    "/", "/callback", "/health", "/api/features",
    "/assets/x.js", "/main.css", "/logo.svg",
    "/v1/workers/register", "/v1/worker/execute",
])
def test_public_paths(path):
    assert m._is_public_path(path) is True


@pytest.mark.parametrize("path", ["/prompt", "/api/prompt", "/userdata/x", "/v1/compute/config"])
def test_protected_paths(path):
    assert m._is_public_path(path) is False


def test_video_never_public():
    """A tenant video is only ever served by /v1/library/file?path=… (the URL path never
    ends in a media suffix), so the suffix bypass cannot apply. ".mp4" must NEVER join the
    static-suffix allowlist, and the org-scoped file route stays auth-gated — pinned here."""
    assert m._is_public_path("/a/b.mp4") is False
    assert m._is_public_path("/v1/library/file") is False


# --- server-owned login entry point (/login → OIDC code flow) ---

def test_login_path_is_public():
    # /login is served by the middleware's own handler, so it must bypass the
    # token gate (it is the very thing an unauthenticated user clicks).
    assert m._is_public_path("/login") is True


def test_middleware_exposes_login_and_callback_handlers():
    mw = m.create_iam_auth_middleware("https://hanzo.id/")
    assert callable(getattr(mw, "handle_login", None))
    assert callable(getattr(mw, "handle_callback", None))


def test_handle_login_unauthenticated_starts_oidc_code_flow():
    """The SPA button hits /login; the server builds the ONE authorize URL —
    client_id=hanzo-studio, redirect_uri=<origin>/callback — never a root redirect."""
    import asyncio
    from aiohttp.test_utils import make_mocked_request

    mw = m.create_iam_auth_middleware("https://hanzo.id")
    req = make_mocked_request(
        "GET", "/login",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "studio.hanzo.ai"},
    )
    resp = asyncio.run(mw.handle_login(req))
    assert resp.status == 302
    loc = resp.headers["Location"]
    assert loc.startswith("https://hanzo.id/v1/iam/oauth/authorize?")
    assert "client_id=hanzo-studio" in loc
    # aiohttp returns the Location header URL-decoded; the wire value is %-encoded.
    assert "redirect_uri=https://studio.hanzo.ai/callback" in loc
    # The pending-login state cookie is set so /callback can verify PKCE + CSRF.
    assert m._STATE_COOKIE in resp.cookies


def test_handle_login_returns_to_app_root_not_the_login_path():
    """Post-login must land on the app root ('/'), never bounce back to '/login'
    (which would re-trigger the flow). The signed return path proves it."""
    import asyncio
    from aiohttp.test_utils import make_mocked_request

    mw = m.create_iam_auth_middleware("https://hanzo.id")
    req = make_mocked_request(
        "GET", "/login",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "studio.hanzo.ai"},
    )
    resp = asyncio.run(mw.handle_login(req))
    cookie = resp.cookies[m._STATE_COOKIE].value
    # State is HMAC-signed with the client_id key (see _sign_state usage).
    payload = m._verify_state(cookie, "hanzo-studio")
    assert payload is not None and payload["t"] == "/"


# --- browser-vs-API gate: anon navigation → 302 /login, XHR/JSON → 401 ---

def _gate(headers):
    """Run the auth middleware for an unauthenticated GET /studio with the given
    request headers; return the response. localhost_bypass is off so the token
    gate actually runs (prod sits behind the ingress, never loopback)."""
    mw = m.create_iam_auth_middleware("https://hanzo.id", localhost_bypass=False)

    async def _ok(_request):
        return web.Response(text="ok")

    req = make_mocked_request("GET", "/studio", headers=headers)
    return asyncio.run(mw(req, _ok))


def test_anon_browser_navigation_redirects_to_login():
    """A top-level browser navigation (Accept: text/html) to a protected page is
    bounced to the server-owned /login entry point, carrying ?next so the OIDC
    round trip returns to /studio — never a raw 401."""
    resp = _gate({"Accept": "text/html,application/xhtml+xml"})
    assert resp.status == 302
    loc = resp.headers["Location"]
    assert loc.startswith("/login?")
    assert parse_qs(urlsplit(loc).query)["next"] == ["/studio"]


def test_anon_plain_curl_redirects_to_login():
    """The confirmed repro: a bare client (Accept: */*, no fetch-metadata) is
    treated as a navigation and redirected, not 401'd."""
    resp = _gate({"Accept": "*/*"})
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login?next=")


def test_anon_no_accept_header_redirects_to_login():
    """No Accept header at all is still ambiguous, not an API call → redirect."""
    resp = _gate({})
    assert resp.status == 302
    assert "/login" in resp.headers["Location"]


def test_anon_sec_fetch_navigate_redirects():
    """Modern browsers stamp Sec-Fetch-Mode: navigate on top-level loads."""
    resp = _gate({"Accept": "*/*", "Sec-Fetch-Mode": "navigate"})
    assert resp.status == 302
    assert "/login" in resp.headers["Location"]


def test_anon_xhr_gets_401():
    """An XHR (X-Requested-With) must get a JSON 401 so the SPA can react, not a
    redirect it would follow into HTML."""
    resp = _gate({"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"})
    assert resp.status == 401


def test_anon_json_accept_gets_401():
    """An explicit JSON client (Accept: application/json) is an API caller."""
    resp = _gate({"Accept": "application/json"})
    assert resp.status == 401


def test_anon_fetch_cors_gets_401():
    """A programmatic fetch() carries Sec-Fetch-Mode: cors — 401, not redirect."""
    resp = _gate({"Accept": "*/*", "Sec-Fetch-Mode": "cors"})
    assert resp.status == 401


def test_is_api_request_predicate():
    def api(headers):
        return m._is_api_request(make_mocked_request("GET", "/studio", headers=headers))

    assert api({"Accept": "*/*"}) is False                      # plain curl → nav
    assert api({"Accept": "text/html"}) is False                # browser → nav
    assert api({}) is False                                     # unknown → nav
    assert api({"Sec-Fetch-Mode": "navigate"}) is False         # top-level nav
    assert api({"Sec-Fetch-Mode": "cors"}) is True              # fetch/XHR
    assert api({"Sec-Fetch-Mode": "same-origin"}) is True       # fetch/XHR
    assert api({"X-Requested-With": "XMLHttpRequest"}) is True  # legacy AJAX
    assert api({"Accept": "application/json"}) is True          # JSON client


def test_safe_next_predicate():
    assert m._safe_next("/studio") == "/studio"
    assert m._safe_next("/studio?org=acme&run=1") == "/studio?org=acme&run=1"
    assert m._safe_next(None) == "/"
    assert m._safe_next("") == "/"
    assert m._safe_next("//evil.example/x") == "/"       # protocol-relative
    assert m._safe_next("https://evil.example") == "/"   # absolute
    assert m._safe_next("/\\evil.example") == "/"        # backslash-relative
    assert m._safe_next("/login") == "/"                 # would loop
    assert m._safe_next("/login?next=/x") == "/"         # would loop


def test_handle_login_honors_next_param():
    """A redirect from the auth gate carries ?next=/studio; login must return the
    user there after the OIDC round trip (the signed state proves the target)."""
    mw = m.create_iam_auth_middleware("https://hanzo.id")
    req = make_mocked_request(
        "GET", "/login?" + urlencode({"next": "/studio"}),
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "studio.hanzo.ai"},
    )
    resp = asyncio.run(mw.handle_login(req))
    assert resp.status == 302
    payload = m._verify_state(resp.cookies[m._STATE_COOKIE].value, "hanzo-studio")
    assert payload["t"] == "/studio"


def test_handle_login_rejects_open_redirect_next():
    """An off-origin, protocol-relative or self-referential next is dropped to
    '/', closing the open-redirect and login-loop vectors."""
    mw = m.create_iam_auth_middleware("https://hanzo.id")
    for bad in ("//evil.example/x", "https://evil.example", "/login"):
        req = make_mocked_request(
            "GET", "/login?" + urlencode({"next": bad}),
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "studio.hanzo.ai"},
        )
        resp = asyncio.run(mw.handle_login(req))
        payload = m._verify_state(resp.cookies[m._STATE_COOKIE].value, "hanzo-studio")
        assert payload["t"] == "/", f"next={bad!r} should be rejected"
