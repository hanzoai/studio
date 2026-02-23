"""
Commerce billing integration for Hanzo Studio.

Records prompt execution usage to the Commerce billing API.
Optionally checks balance before accepting expensive operations.

Commerce API: POST /api/v1/billing/usage
Commerce API: GET  /api/v1/billing/balance
"""
import logging
import os
import time as _time

import aiohttp

# Commerce endpoint config (from env vars)
COMMERCE_URL = os.environ.get(
    "STUDIO_COMMERCE_URL",
    os.environ.get("COMMERCE_ENDPOINT", "http://commerce.hanzo.svc:8001"),
)
COMMERCE_TOKEN = os.environ.get(
    "STUDIO_COMMERCE_TOKEN",
    os.environ.get("COMMERCE_TOKEN", ""),
)

_shared_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Authorization": f"Bearer {COMMERCE_TOKEN}"},
        )
    return _shared_session


async def record_usage(
    user: str,
    org_id: str,
    prompt_id: str,
    execution_time: float,
    success: bool,
    node_count: int = 0,
) -> None:
    """
    Record prompt execution usage to Commerce billing API.
    Fire-and-forget — errors are logged but do not block the response.

    Pricing: 1 credit per prompt execution second (rounded up).
    Minimum 1 credit per execution.
    """
    if not COMMERCE_TOKEN:
        return

    # Calculate cost: 1 cent per second of execution, min 1 cent
    amount = max(1, int(execution_time + 0.5))

    url = f"{COMMERCE_URL.rstrip('/')}/api/v1/billing/usage"
    payload = {
        "user": f"{org_id}/{user}" if org_id else user,
        "currency": "usd",
        "amount": amount,
        "model": "studio",
        "provider": "hanzo-studio",
        "requestId": prompt_id,
        "status": "success" if success else "error",
        "promptTokens": node_count,
        "completionTokens": 0,
        "totalTokens": node_count,
    }

    try:
        session = await _get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logging.warning(f"Commerce billing error {resp.status}: {body}")
    except Exception as e:
        logging.warning(f"Commerce billing request failed: {e}")


async def check_balance(user: str, org_id: str) -> tuple[bool, int]:
    """
    Check if user/org has positive balance. Returns (has_balance, available_cents).
    Returns (True, -1) if billing is not configured (allowing all requests).
    """
    if not COMMERCE_TOKEN:
        return True, -1

    url = f"{COMMERCE_URL.rstrip('/')}/api/v1/billing/balance"
    params = {
        "user": f"{org_id}/{user}" if org_id else user,
        "currency": "usd",
    }

    try:
        session = await _get_session()
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                # If billing service is down, allow the request
                logging.warning(f"Commerce balance check returned {resp.status}")
                return True, -1
            data = await resp.json()
            available = data.get("available", 0)
            return available > 0, available
    except Exception as e:
        logging.warning(f"Commerce balance check failed: {e}")
        return True, -1
