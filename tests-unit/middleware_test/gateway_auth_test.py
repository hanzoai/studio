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


def test_symmetric_jwt_server_key_is_skipped_not_returned(monkeypatch):
    """THE CHAT OUTAGE: an HS256 *commerce* service token sat in the AI server-key slot.
    It "won" precedence and every chat 502'd ("unsupported signing method: HS256") while
    the caller's own RS256 IAM token — which the gateway accepts — was never tried. A
    provably-unverifiable server key must be SKIPPED so the working path takes over."""
    envs = ("BAD_TOK", "GOOD_TOK")
    for e in envs:
        monkeypatch.delenv(e, raising=False)
    hs, rs = _jwt("HS256"), _jwt("RS256")
    assert G.unverifiable(hs) is True
    assert G.unverifiable(rs) is False
    assert G.unverifiable("hk-opaque-gateway-key") is False   # opaque: only the gateway judges
    # An HS256 server key alone: skipped → the caller's verifiable token is used instead.
    monkeypatch.setenv("BAD_TOK", hs)
    assert G.server_token(envs) == ""
    assert G.ai_bearer(_Req("Bearer " + rs), server_env=envs) == "Bearer " + rs
    # …and with no usable caller token either, it degrades honestly (never a doomed 502).
    assert G.ai_bearer(_Req("Bearer " + _jwt("HS256")), server_env=envs) == ""
    # A verifiable later key is still found past the skipped one.
    monkeypatch.setenv("GOOD_TOK", "pk-live-abc")
    assert G.server_token(envs) == "Bearer pk-live-abc"


def test_ai_lanes_never_consult_a_commerce_token():
    """One credential, one audience: the commerce service token is not an AI credential.
    Listing it in an AI server_env is what caused the outage above."""
    from middleware import copilot, documents
    for envs in (copilot._SERVER_TOKEN_ENV, documents._SERVER_TOKEN_ENV):
        assert "STUDIO_COMMERCE_TOKEN" not in envs, envs
        assert envs, "an AI lane still needs its own server-key env"


def test_forwardable_bearer_survives_crafted_non_string_alg():
    """A JWT header with a non-string alg (null/list/number/dict) must yield "" — never
    a self-inflicted 500 from alg[:2]/alg.startswith. Mirrors the documents.py RED fix."""
    def jwt_bad_alg(alg_value):
        h = base64.urlsafe_b64encode(json.dumps({"alg": alg_value}).encode()).rstrip(b"=").decode()
        return f"{h}.eyJzdWIiOiIxIn0.sig"
    for bad in (None, [], 123, {"x": 1}, True):
        assert G.jwt_alg(jwt_bad_alg(bad)) == ""
        assert G.forwardable_bearer(_Req("Bearer " + jwt_bad_alg(bad))) == ""
    assert G.jwt_alg(jwt_bad_alg("RS256")) == "RS256"  # real asymmetric alg still read
    assert G.forwardable_bearer(_Req("Bearer " + jwt_bad_alg("RS256"))) == "Bearer " + jwt_bad_alg("RS256")


def test_alg_allowlist_is_the_exact_jws_set_not_a_prefix_match():
    """F3: a prefix match ("RS"/"Ed") admitted non-JWS names like RSA-OAEP / 'Edwina';
    the allowlist is now the exact JWS asymmetric set."""
    for good in ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512", "EdDSA"):
        assert G.forwardable_bearer(_Req("Bearer " + _jwt(good))) == "Bearer " + _jwt(good)
    for bad in ("RSA-OAEP", "RSA1_5", "Edwina", "ES", "RS", "none", "HS256", "PSK"):
        assert G.forwardable_bearer(_Req("Bearer " + _jwt(bad))) == ""
