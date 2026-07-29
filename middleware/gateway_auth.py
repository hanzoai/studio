"""Gateway auth — the ONE fail-closed allowlist for handing a browser-edge bearer to
the Hanzo AI gateway (api.hanzo.ai/v1/chat/completions).

The problem it closes: the browser session is a *symmetric* HS256 hanzo.id token. The
gateway verifies bearers against JWKS and refuses HS256 ("unsupported signing method:
HS256") — so forwarding the raw session token produces a 502, not a chat. Secret keys
(``sk-``/``hk-``/``rk-``) must never leave the server, and a publishable ``pk-`` key is
the one client-safe family the gateway accepts from the edge.

So the rule, fail-closed, in ONE place every gateway caller reuses:
  1. a server-side gateway key (configured, never in page source) wins — but ONLY when it
     is not provably unverifiable. A symmetric-JWT server key is refused for the same
     reason a session token is: the gateway rejects it. A "winning" key that can only 502
     is worse than no key, because it MASKS the caller's own working token (this is
     exactly how an HS256 *commerce* credential in the AI slot took studio chat down:
     every request 502'd while the user's own RS256 IAM token would have been accepted).
  2. else the caller's own token — forwarded ONLY when the gateway can verify it: a
     publishable ``pk-`` key, or an asymmetric/JWKS JWT (RS/ES/PS/EdDSA);
  3. an HS256 session token or any secret key is refused → "" (the caller then degrades
     honestly instead of emitting a 502).

One credential, one audience: a service token for commerce (or any other service) is NOT
an AI-gateway credential and must never be listed in an ``server_env`` here.

This is the same shape the office-documents AI-fill proved (documents.py); it lives here
so copilot and any future gateway caller share exactly one implementation.
"""
from __future__ import annotations

import base64
import json
import os

# Secret key families — must never reach the gateway from the browser edge, nor appear
# in page source. Matched case-insensitively on the bare token.
_SECRET_PREFIXES = ("sk-", "sk_", "hk-", "hk_", "rk-", "rk_")


def jwt_alg(tok: str) -> str:
    """The ``alg`` from a JWT header, or "" if not a decodable JWT. Distinguishes a
    JWKS-verifiable asymmetric token (gateway-acceptable) from an HS256 session token."""
    try:
        h = tok.split(".", 1)[0]
        h += "=" * (-len(h) % 4)
        alg = json.loads(base64.urlsafe_b64decode(h.encode())).get("alg", "")
        # A crafted header can make alg a non-string (null/list/number/dict); the
        # contract is "the alg string or ''", so a non-string is not a usable alg
        # (else `alg[:2]`/`alg.startswith` below would raise → a self-inflicted 500).
        return alg if isinstance(alg, str) else ""
    except Exception:
        return ""


def forwardable_bearer(request) -> str:
    """The caller's own token to forward to the gateway, or "" — fail-closed.

    Reuses the app's one token path (``gpu_dispatch._bearer``: Authorization header or the
    browser session cookie), then applies the strict allowlist:
      * a publishable ``pk-`` key — allowed;
      * a JWT — allowed ONLY when asymmetric/JWKS-verifiable (RS/ES/PS/EdDSA); a symmetric
        HS256 hanzo.id session token is refused (the gateway rejects it, so forwarding it
        only produces log noise and a 502);
      * a secret ``sk-``/``hk-``/``rk-`` key — refused outright.
    So a secret can never reach the gateway from the browser edge and none is in page
    source. Returns a full ``Bearer <tok>`` value, or "".
    """
    from middleware.gpu_dispatch import _bearer  # lazy: keeps this module import light
    raw = _bearer(request)
    if not raw:
        return ""
    tok = raw[7:].strip() if raw[:7].lower() == "bearer " else raw.strip()
    if not tok or tok.lower().startswith(_SECRET_PREFIXES):
        return ""
    if tok.startswith(("pk-", "pk_")):
        return "Bearer " + tok
    # Exact JWS asymmetric set — a prefix match ("RS"/"Ed") also admitted non-JWS names
    # like RSA-OAEP / "Edwina"; `alg in <tuple>` is crash-safe for any type.
    if tok.count(".") == 2 and jwt_alg(tok) in _ASYMMETRIC:
        return "Bearer " + tok
    return ""


_ASYMMETRIC = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512",
               "PS256", "PS384", "PS512", "EdDSA")


def unverifiable(tok: str) -> bool:
    """True when the gateway PROVABLY cannot verify this token: a JWT whose ``alg`` is not
    in the JWKS-verifiable asymmetric set (an HS256 session or service token). An opaque
    non-JWT key (a raw gateway/publishable key) is not provably unverifiable, so it is not
    refused here — only the gateway can judge it."""
    return tok.count(".") == 2 and jwt_alg(tok) not in _ASYMMETRIC


def server_token(env_names) -> str:
    """The first configured server-side gateway key from ``env_names`` that the gateway
    could actually accept, as a full ``Bearer <tok>`` value, or "". Server-side only —
    never emitted to page source.

    A configured key that is a symmetric JWT is SKIPPED, not returned: it can only produce
    "unsupported signing method: HS256" at the gateway, and returning it would mask the
    caller's own verifiable token. Misconfiguration degrades to the working path instead of
    taking the feature down."""
    for env in env_names:
        v = os.environ.get(env, "").strip()
        if not v:
            continue
        tok = v[7:].strip() if v[:7].lower() == "bearer " else v
        if unverifiable(tok):
            continue
        return "Bearer " + tok
    return ""


def ai_bearer(request, *, server_env) -> str:
    """The bearer to send to the gateway for this request, or "" when none is usable.

    A server-side gateway key wins (the configured, working path); otherwise the caller's
    own gateway-verifiable token via the fail-closed allowlist. Returns ``Bearer <tok>``
    or "". Callers that need the bare token can strip the 7-char ``Bearer `` prefix."""
    return server_token(server_env) or forwardable_bearer(request)
