"""
IAM authentication middleware for Hanzo Studio.

Validates Bearer tokens against hanzo.id (Casdoor) /api/get-account endpoint.
Localhost connections bypass auth. Static assets and health checks are exempt.
"""
import logging
import os
import ipaddress

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
    "/api/system_stats",
    "/api/features",
}


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


def create_iam_auth_middleware(iam_url: str, localhost_bypass: bool = True):
    """
    Factory that creates an aiohttp middleware for IAM token validation.

    Args:
        iam_url: Base URL of the IAM server (e.g. https://hanzo.id)
        localhost_bypass: If True, skip auth for loopback connections
    """
    iam_url = iam_url.rstrip("/")
    account_endpoint = f"{iam_url}/api/get-account"

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

        # Validate token against IAM
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    account_endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"error": "Invalid or expired token"},
                            status=401,
                        )
                    user_info = await resp.json()
        except Exception as e:
            logging.error(f"IAM validation error: {e}")
            return web.json_response(
                {"error": "Authentication service unavailable"},
                status=503,
            )

        # Extract user/org context from IAM response
        # Casdoor returns: id, name, owner (org), email, etc.
        request["iam_user"] = {
            "sub": user_info.get("id", user_info.get("sub", "")),
            "name": user_info.get("name", user_info.get("displayName", "")),
            "email": user_info.get("email", ""),
            "org_id": user_info.get("owner", user_info.get("organization", "default")),
            "avatar": user_info.get("avatar", ""),
        }

        return await handler(request)

    return iam_auth_middleware
