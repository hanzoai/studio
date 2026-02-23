"""
IAM authentication middleware for Hanzo Studio.

Validates Bearer tokens against hanzo.id (Casdoor) /api/get-account endpoint.
Localhost connections bypass auth. Static assets and health checks are exempt.
"""
import logging
import os
import ipaddress
import hashlib
import time as _time

import aiohttp
from aiohttp import web

# Paths that never require auth (static assets, health, ws negotiation)
PUBLIC_PATH_PREFIXES = (
    "/favicon",
    "/assets/",
    "/extensions/",
    "/templates/",
    "/docs/",
)

PUBLIC_PATHS_EXACT = {
    "/",
    "/health",
    "/ready",
    "/metrics",
    "/api/health",
    "/api/ready",
    "/api/features",
    "/api/metrics",
}

# Token validation cache: hash(token) -> (user_info, expires_at)
_token_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 60  # seconds


def _is_loopback_request(request: web.Request) -> bool:
    """Check if the request originates from a loopback address."""
    peername = request.transport.get_extra_info("peername")
    if peername is None:
        return False
    host = peername[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_public_path(path: str) -> bool:
    """Check if the path is publicly accessible without auth."""
    if path in PUBLIC_PATHS_EXACT:
        return True
    for prefix in PUBLIC_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    # Static file extensions served from web root
    if any(path.endswith(ext) for ext in (".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map", ".webm")):
        return True
    return False


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def create_iam_auth_middleware(iam_url: str, localhost_bypass: bool = True):
    """
    Factory that creates an aiohttp middleware for IAM token validation.

    Args:
        iam_url: Base URL of the IAM server (e.g. https://hanzo.id)
        localhost_bypass: If True, skip auth for loopback connections
    """
    iam_url = iam_url.rstrip("/")
    account_endpoint = f"{iam_url}/api/get-account"

    # Shared session — created lazily, reused across requests
    _shared_session: aiohttp.ClientSession | None = None

    async def _get_session() -> aiohttp.ClientSession:
        nonlocal _shared_session
        if _shared_session is None or _shared_session.closed:
            _shared_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return _shared_session

    @web.middleware
    async def iam_auth_middleware(request: web.Request, handler):
        path = request.path

        # Always allow public paths (static assets, health checks)
        if _is_public_path(path):
            return await handler(request)

        # Localhost bypass for dev mode
        if localhost_bypass and _is_loopback_request(request):
            request["iam_user"] = {
                "sub": "local",
                "name": "Local User",
                "org_id": os.environ.get("STUDIO_ORG_ID", "default"),
            }
            return await handler(request)

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # Also check cookie for browser sessions
        if not token:
            token = request.cookies.get("hanzo_token") or request.cookies.get("access_token")

        # Also check query param for WebSocket upgrade
        if not token and request.path == "/ws":
            token = request.rel_url.query.get("token")

        if not token:
            # Return login redirect for browser requests, 401 for API
            accept = request.headers.get("Accept", "")
            if "text/html" in accept:
                raise web.HTTPFound(f"{iam_url}/login?redirect_uri={request.url}")
            return web.json_response(
                {"error": "Authentication required", "iam_url": iam_url},
                status=401,
            )

        # Check token cache first
        ck = _cache_key(token)
        cached = _token_cache.get(ck)
        if cached and cached[1] > _time.monotonic():
            request["iam_user"] = cached[0]
            return await handler(request)

        # Validate token against IAM
        try:
            session = await _get_session()
            async with session.get(
                account_endpoint,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    _token_cache.pop(ck, None)
                    return web.json_response(
                        {"error": "Invalid or expired token"},
                        status=401,
                    )
                raw = await resp.json()
        except Exception as e:
            logging.error(f"IAM validation error: {e}")
            return web.json_response(
                {"error": "Authentication service unavailable"},
                status=503,
            )

        # Extract user/org context from IAM response
        # Casdoor returns: id, name, owner (org), email, etc.
        user_info = {
            "sub": raw.get("id", raw.get("sub", "")),
            "name": raw.get("name", raw.get("displayName", "")),
            "email": raw.get("email", ""),
            "org_id": raw.get("owner", raw.get("organization", "default")),
            "avatar": raw.get("avatar", ""),
        }

        # Cache the validated token
        _token_cache[ck] = (user_info, _time.monotonic() + _CACHE_TTL)

        # Evict stale entries periodically (simple, not perfect)
        if len(_token_cache) > 1000:
            now = _time.monotonic()
            stale = [k for k, v in _token_cache.items() if v[1] < now]
            for k in stale:
                del _token_cache[k]

        request["iam_user"] = user_info
        return await handler(request)

    return iam_auth_middleware
