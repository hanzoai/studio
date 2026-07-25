"""Gateway auth — the ONE fail-closed allowlist for handing a browser-edge bearer to
the Hanzo AI gateway (api.hanzo.ai/v1/chat/completions).

The problem it closes: the browser session is a *symmetric* HS256 hanzo.id token. The
gateway verifies bearers against JWKS and refuses HS256 ("unsupported signing method:
HS256") — so forwarding the raw session token produces a 502, not a chat. Secret keys
(``sk-``/``hk-``/``rk-``) must never leave the server, and a publishable ``pk-`` key is
the one client-safe family the gateway accepts from the edge.

So the rule, fail-closed, in ONE place every gateway caller reuses:
  1. a server-side gateway key (configured, never in page source) wins;
  2. else the caller's own token — forwarded ONLY when the gateway can verify it: a
     publishable ``pk-`` key, or an asymmetric/JWKS JWT (RS/ES/PS/EdDSA);
  3. an HS256 session token or any secret key is refused → "" (the caller then degrades
     honestly instead of emitting a 502).

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
    if tok.count(".") == 2:
        alg = jwt_alg(tok)
        if isinstance(alg, str) and (alg[:2] in ("RS", "ES", "PS") or alg.startswith("Ed")):
            return "Bearer " + tok
    return ""


def server_token(env_names) -> str:
    """The first configured server-side gateway key from ``env_names``, as a full
    ``Bearer <tok>`` value, or "". Server-side only — never emitted to page source."""
    for env in env_names:
        v = os.environ.get(env, "").strip()
        if v:
            return v if v[:7].lower() == "bearer " else "Bearer " + v
    return ""


def ai_bearer(request, *, server_env) -> str:
    """The bearer to send to the gateway for this request, or "" when none is usable.

    A server-side gateway key wins (the configured, working path); otherwise the caller's
    own gateway-verifiable token via the fail-closed allowlist. Returns ``Bearer <tok>``
    or "". Callers that need the bare token can strip the 7-char ``Bearer `` prefix."""
    return server_token(server_env) or forwardable_bearer(request)
