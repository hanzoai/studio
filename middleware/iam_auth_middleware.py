"""
IAM authentication middleware for Hanzo Studio.

Validates Bearer/cookie JWTs locally against the Hanzo IAM JWKS
(``{iam_url}/v1/iam/.well-known/jwks``) — no per-request network call once the
signing keys are cached. Signature (RS256 by default), ``exp``, ``iss`` and
``aud`` are all verified. The organization is taken from the ``owner`` claim
(``organization`` fallback) and exposed as ``request["iam_user"]`` for the
tenancy / rate-limit / billing paths.

Browser sessions use the standard OIDC Authorization Code flow: an
unauthenticated HTML request is redirected to the IAM ``authorize`` endpoint;
IAM bounces back to ``/callback`` which exchanges the code for a token and sets
an httpOnly session cookie. No frontend changes are required — the prebuilt SPA
just rides the cookie.

Localhost connections bypass auth (single-user dev). Static assets, health
checks and the worker coordinator endpoints are exempt.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time as _time
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# Paths that never require auth (static assets, health, ws negotiation, oauth).
PUBLIC_PATH_PREFIXES = (
    "/favicon",
    "/assets/",
    "/extensions/",
    "/templates/",
    "/docs/",
)

PUBLIC_PATHS_EXACT = {
    "/",
    "/callback",
    "/health",
    "/ready",
    "/metrics",
    "/api/health",
    "/api/ready",
    "/api/features",
    "/api/metrics",
    # Worker registration / job exchange use their own coordinator trust
    # (STUDIO_WORKER_TOKEN); see worker_client and the federation design doc.
    "/v1/workers/register",
    "/api/v1/workers/register",
    "/v1/jobs/claim",
    "/api/v1/jobs/claim",
    "/v1/worker/execute",
    "/api/v1/worker/execute",
}

_OIDC_SCOPES = "openid profile email"
_STATE_COOKIE = "studio_oauth"
_SESSION_COOKIE = "hanzo_token"
_STATE_TTL = 600  # seconds a pending login may take

# Decoded-claims cache: hash(token) -> (user_info, expires_at_monotonic)
_token_cache: dict[str, tuple[dict, float]] = {}


def _is_loopback_request(request: web.Request) -> bool:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if peername is None:
        return False
    try:
        return ipaddress.ip_address(peername[0]).is_loopback
    except ValueError:
        return False


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS_EXACT:
        return True
    if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return True
    return any(path.endswith(ext) for ext in (
        ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map", ".webm",
    ))


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _external_base(request: web.Request) -> str:
    """Public origin of this Studio, honoring the ingress forwarded headers."""
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


class _JwksCache:
    """Async-fetched, kid-indexed JWKS with rate-limited refresh on cache miss."""

    def __init__(self, jwks_uri: str, ttl: float = 600.0, min_refresh: float = 30.0):
        self._uri = jwks_uri
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def _refresh(self) -> None:
        from jwt import PyJWKSet
        session = await self._session_get()
        async with session.get(self._uri) as resp:
            if resp.status != 200:
                raise RuntimeError(f"JWKS fetch {resp.status} from {self._uri}")
            data = await resp.json()
        jwk_set = PyJWKSet.from_dict(data)
        self._keys = {k.key_id: k.key for k in jwk_set.keys if k.key_id}
        self._fetched_at = _time.monotonic()

    async def get_key(self, kid: str):
        now = _time.monotonic()
        stale = (now - self._fetched_at) > self._ttl
        if (kid not in self._keys or stale) and (now - self._fetched_at) > self._min_refresh:
            await self._refresh()
        return self._keys.get(kid)


def _sign_state(payload: dict, secret: str) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64url(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def _verify_state(blob: str, secret: str) -> dict | None:
    try:
        body, mac = blob.split(".", 1)
    except ValueError:
        return None
    expected = _b64url(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return None
    if payload.get("exp", 0) < _time.time():
        return None
    return payload


# IAM signs RS256 by default; ES/RS variants are selectable per application.
_ALGS = ["RS256", "RS512", "ES256", "ES384", "ES512"]


def _claims_to_user(claims: dict) -> dict:
    return {
        "sub": claims.get("sub", ""),
        "name": claims.get("name") or claims.get("preferred_username", ""),
        "email": claims.get("email", ""),
        "org_id": claims.get("owner") or claims.get("organization") or "default",
        "avatar": claims.get("picture", ""),
    }


def verify_jwt(token: str, key, issuer: str, audience: str | None) -> dict | None:
    """
    Pure JWT verification: signature (via the resolved JWKS key), exp, iss and
    aud. Returns normalized user info or None if the token is invalid. No I/O.
    """
    import jwt
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=_ALGS,
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss"], "verify_aud": bool(audience)},
        )
    except jwt.InvalidTokenError:
        return None
    return _claims_to_user(claims)


def create_iam_auth_middleware(iam_url: str, localhost_bypass: bool = True):
    """
    Build the aiohttp middleware that validates IAM JWTs via JWKS.

    Config is read from the environment (KMS-sourced in cloud):
      STUDIO_IAM_CLIENT_ID     OAuth client_id / expected audience (default hanzo-studio)
      STUDIO_IAM_CLIENT_SECRET confidential-client secret for the code exchange
      STUDIO_IAM_ISSUER        expected iss (default = iam_url, e.g. https://hanzo.id)
    """
    iam_url = iam_url.rstrip("/")
    client_id = os.environ.get("STUDIO_IAM_CLIENT_ID", "hanzo-studio")
    issuer = os.environ.get("STUDIO_IAM_ISSUER", iam_url)
    jwks_uri = f"{iam_url}/v1/iam/.well-known/jwks"
    jwks = _JwksCache(jwks_uri)

    async def _validate(token: str) -> dict | None:
        """Return normalized user info for a valid token, else None."""
        ck = _cache_key(token)
        cached = _token_cache.get(ck)
        if cached and cached[1] > _time.monotonic():
            return cached[0]

        import jwt
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None
        kid = header.get("kid")
        if not kid:
            return None
        try:
            key = await jwks.get_key(kid)
        except Exception as e:
            logger.error("JWKS fetch failed: %s", e)
            raise
        if key is None:
            return None

        user = verify_jwt(token, key, issuer, client_id)
        if user is None:
            _token_cache.pop(ck, None)
            return None

        # Cache the validated result, capped at 60s so revocation propagates.
        _token_cache[ck] = (user, _time.monotonic() + 60.0)
        if len(_token_cache) > 2000:
            now = _time.monotonic()
            for k in [k for k, v in _token_cache.items() if v[1] < now]:
                _token_cache.pop(k, None)
        return user

    def _authorize_redirect(request: web.Request) -> web.Response:
        base = _external_base(request)
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        nonce = secrets.token_urlsafe(16)
        state_payload = {
            "n": nonce,
            "v": verifier,
            "t": str(request.rel_url),
            "exp": int(_time.time()) + _STATE_TTL,
        }
        state = _sign_state(state_payload, client_id)  # HMAC key; secret optional
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": f"{base}/callback",
            "scope": _OIDC_SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        resp = web.HTTPFound(f"{iam_url}/v1/iam/oauth/authorize?{urlencode(params)}")
        resp.set_cookie(
            _STATE_COOKIE, state, max_age=_STATE_TTL, httponly=True,
            secure=base.startswith("https"), samesite="Lax", path="/",
        )
        return resp

    async def handle_callback(request: web.Request) -> web.Response:
        """Exchange the authorization code for a token and set the session cookie."""
        code = request.query.get("code")
        state = request.query.get("state")
        cookie_state = request.cookies.get(_STATE_COOKIE)
        if not code or not state or state != cookie_state:
            return web.Response(status=400, text="Invalid OAuth state")
        payload = _verify_state(state, client_id)
        if payload is None:
            return web.Response(status=400, text="Expired or forged OAuth state")

        base = _external_base(request)
        client_secret = os.environ.get("STUDIO_IAM_CLIENT_SECRET", "")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{base}/callback",
            "client_id": client_id,
            "code_verifier": payload["v"],
        }
        if client_secret:
            form["client_secret"] = client_secret
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(f"{iam_url}/v1/iam/oauth/token", data=form) as r:
                    if r.status != 200:
                        logger.error("Token exchange failed %s: %s", r.status, await r.text())
                        return web.Response(status=502, text="Token exchange failed")
                    tok = await r.json()
        except Exception as e:
            logger.error("Token exchange error: %s", e)
            return web.Response(status=502, text="IAM unreachable")

        access = tok.get("access_token")
        if not access or await _validate(access) is None:
            return web.Response(status=502, text="Invalid token from IAM")

        target = payload.get("t", "/")
        if not target.startswith("/"):
            target = "/"
        resp = web.HTTPFound(target)
        resp.set_cookie(
            _SESSION_COOKIE, access, max_age=int(tok.get("expires_in", 3600)),
            httponly=True, secure=base.startswith("https"), samesite="Lax", path="/",
        )
        resp.del_cookie(_STATE_COOKIE, path="/")
        return resp

    @web.middleware
    async def iam_auth_middleware(request: web.Request, handler):
        path = request.path

        if _is_public_path(path):
            return await handler(request)

        if localhost_bypass and _is_loopback_request(request):
            request["iam_user"] = {
                "sub": "local", "name": "Local User", "email": "",
                "org_id": os.environ.get("STUDIO_ORG_ID", "default"), "avatar": "",
            }
            return await handler(request)

        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.cookies.get(_SESSION_COOKIE) or request.cookies.get("access_token")
        if not token and path == "/ws":
            token = request.rel_url.query.get("token")

        wants_html = "text/html" in request.headers.get("Accept", "")

        if not token:
            if wants_html:
                return _authorize_redirect(request)
            return web.json_response({"error": "Authentication required", "iam_url": iam_url}, status=401)

        try:
            user = await _validate(token)
        except Exception:
            return web.json_response({"error": "Authentication service unavailable"}, status=503)

        if user is None:
            if wants_html:
                return _authorize_redirect(request)
            return web.json_response({"error": "Invalid or expired token"}, status=401)

        request["iam_user"] = user
        return await handler(request)

    # Expose the callback handler so server.py can register the /callback route.
    iam_auth_middleware.handle_callback = handle_callback
    return iam_auth_middleware
