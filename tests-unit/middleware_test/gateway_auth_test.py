"""The ONE fail-closed gateway-bearer allowlist (middleware/gateway_auth.py).

This is the credential seam that keeps Chat + copilot reaching api.hanzo.ai instead of
502-ing on the browser's HS256 session token. The invariants pinned here:

  * a symmetric HS256 hanzo.id session token is NEVER forwarded (the gateway rejects it —
    "unsupported signing method: HS256");
  * a secret sk-/hk-/rk- key is refused outright (never leaves the server, never in page
    source);
  * only a publishable pk- key or an asymmetric/JWKS JWT (RS/ES/PS/EdDSA) is forwarded;
  * a configured server-side gateway key wins over any caller token.
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import gateway_auth as G


def _jwt(alg: str) -> str:
    """A JWT with the given header alg (payload/sig inert — only the header alg matters)."""
    h = base64.urlsafe_b64encode(json.dumps({"alg": alg, "typ": "JWT"}).encode()).rstrip(b"=").decode()
    return f"{h}.eyJzdWIiOiIxIn0.sig"


class _Req:
    """The 2 attributes gpu_dispatch._bearer reads: Authorization header + cookies."""
    def __init__(self, auth=None, cookies=None):
        self.headers = {"Authorization": auth} if auth else {}
        self.cookies = cookies or {}


def test_forwardable_bearer_is_fail_closed():
    rs, es, ps, ed, hs = _jwt("RS256"), _jwt("ES256"), _jwt("PS256"), _jwt("EdDSA"), _jwt("HS256")
    # asymmetric / JWKS-verifiable JWTs — forwarded
    assert G.forwardable_bearer(_Req("Bearer " + rs)) == "Bearer " + rs
    assert G.forwardable_bearer(_Req("Bearer " + es)) == "Bearer " + es
    assert G.forwardable_bearer(_Req("Bearer " + ps)) == "Bearer " + ps
    assert G.forwardable_bearer(_Req("Bearer " + ed)) == "Bearer " + ed
    # the HS256 hanzo.id session token — REFUSED (this is the 502 the fix closes)
    assert G.forwardable_bearer(_Req("Bearer " + hs)) == ""
    # publishable key — allowed
    assert G.forwardable_bearer(_Req("Bearer pk-publishable")) == "Bearer pk-publishable"
    # secret key families — refused outright, case-insensitively
    for secret in ("sk-secret", "SK-Secret", "hk-secret", "rk-secret", "sk_secret", "hk_secret"):
        assert G.forwardable_bearer(_Req("Bearer " + secret)) == ""
    # no token / opaque non-JWT non-pk value
    assert G.forwardable_bearer(_Req(None)) == ""
    assert G.forwardable_bearer(_Req("Bearer notoken")) == ""


def test_forwardable_bearer_reads_session_cookie():
    # The browser sends the session as a cookie, not an Authorization header. A verifiable
    # JWT there is honored; an HS256 session cookie is still refused.
    rs, hs = _jwt("RS256"), _jwt("HS256")
    assert G.forwardable_bearer(_Req(None, {"access_token": rs})) == "Bearer " + rs
    assert G.forwardable_bearer(_Req(None, {"hanzo_token": rs})) == "Bearer " + rs
    assert G.forwardable_bearer(_Req(None, {"access_token": hs})) == ""


def test_server_token_precedence_and_normalization(monkeypatch):
    envs = ("A_TOK", "B_TOK")
    for e in envs:
        monkeypatch.delenv(e, raising=False)
    assert G.server_token(envs) == ""
    monkeypatch.setenv("B_TOK", "hk-second")
    assert G.server_token(envs) == "Bearer hk-second"     # bare value gets a Bearer prefix
    monkeypatch.setenv("A_TOK", "Bearer hk-first")
    assert G.server_token(envs) == "Bearer hk-first"      # first wins; already-prefixed kept as-is


def test_ai_bearer_server_key_wins_else_forwardable(monkeypatch):
    envs = ("SRV_TOK",)
    monkeypatch.delenv("SRV_TOK", raising=False)
    hs, rs = _jwt("HS256"), _jwt("RS256")
    # no server key + only an HS256 session → nothing usable (fail-closed → honest degrade)
    assert G.ai_bearer(_Req("Bearer " + hs), server_env=envs) == ""
    # no server key + a verifiable caller token → that token is forwarded
    assert G.ai_bearer(_Req("Bearer " + rs), server_env=envs) == "Bearer " + rs
    # a configured server key wins over any caller token
    monkeypatch.setenv("SRV_TOK", "hk-server-key")
    assert G.ai_bearer(_Req("Bearer " + rs), server_env=envs) == "Bearer hk-server-key"
