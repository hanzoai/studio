"""
Rate limiting middleware for Hanzo Studio.

Per-org sliding window rate limiter for prompt submissions.
Tracks requests in-memory using a simple time-bucketed approach.
"""
import logging
import time as _time
import os
import threading

from aiohttp import web


# Default: 60 requests per minute per org
_DEFAULT_RPM = int(os.environ.get("STUDIO_RPM", "0"))  # 0 = unlimited (single-user local default)
_WINDOW_SECONDS = 60

# org_id -> list of request timestamps
_request_log: dict[str, list[float]] = {}
_lock = threading.Lock()


def _cleanup_old(timestamps: list[float], now: float) -> list[float]:
    """Remove timestamps older than the window."""
    cutoff = now - _WINDOW_SECONDS
    # Find first valid index via linear scan (list is chronological)
    i = 0
    while i < len(timestamps) and timestamps[i] < cutoff:
        i += 1
    return timestamps[i:]


def create_rate_limit_middleware(rpm: int = _DEFAULT_RPM):
    """
    Factory that creates rate limiting middleware.

    Args:
        rpm: Maximum requests per minute per org. 0 = unlimited.
    """
    if rpm <= 0:
        # No rate limiting — return passthrough middleware
        @web.middleware
        async def noop_middleware(request: web.Request, handler):
            return await handler(request)
        return noop_middleware

    # Only rate-limit prompt submission and queue mutation endpoints
    RATE_LIMITED_PATHS = {"/prompt", "/api/prompt"}

    @web.middleware
    async def rate_limit_middleware(request: web.Request, handler):
        path = request.path
        method = request.method

        # Only rate limit POST to prompt endpoints
        if method != "POST" or path not in RATE_LIMITED_PATHS:
            return await handler(request)

        # Get org identifier
        iam_user = request.get("iam_user")
        if iam_user:
            org_id = iam_user.get("org_id", "default")
        else:
            org_id = "default"

        now = _time.monotonic()

        with _lock:
            timestamps = _request_log.get(org_id, [])
            timestamps = _cleanup_old(timestamps, now)

            if len(timestamps) >= rpm:
                _request_log[org_id] = timestamps
                oldest = timestamps[0] if timestamps else now
                retry_after = int(_WINDOW_SECONDS - (now - oldest)) + 1
                logging.warning(
                    f"Rate limit exceeded for org {org_id}: "
                    f"{len(timestamps)}/{rpm} rpm"
                )
                return web.json_response(
                    {
                        "error": "Rate limit exceeded",
                        "limit": rpm,
                        "window": _WINDOW_SECONDS,
                        "retry_after": retry_after,
                    },
                    status=429,
                    headers={"Retry-After": str(retry_after)},
                )

            timestamps.append(now)
            _request_log[org_id] = timestamps

        # Periodic cleanup of stale orgs
        if len(_request_log) > 500:
            with _lock:
                stale = [
                    k for k, v in _request_log.items()
                    if not v or v[-1] < now - _WINDOW_SECONDS * 2
                ]
                for k in stale:
                    del _request_log[k]

        return await handler(request)

    return rate_limit_middleware
