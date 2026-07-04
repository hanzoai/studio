"""
Commerce billing integration for Hanzo Studio.

record_render — emits ONE metered usage event per completed render to the
Commerce meter-events API. Fire-and-forget: a billing failure never blocks or
fails a render. Env-gated (all three required; absent locally = no-op):

    STUDIO_BILLING_URL     full URL of POST /v1/billing/meter-events
    STUDIO_COMMERCE_TOKEN  commerce admin bearer (KMS /studio commerce-token)
    STUDIO_BILLING_METER   id of the "studio render" meter (created in Commerce)

Commerce contract — POST /v1/billing/meter-events:
    {"events":[{"meterId","userId","value","idempotency","timestamp","dimensions"}]}
Pricing lives on the meter in Commerce; Studio only reports raw quantity.

check_balance — pre-existing optional pre-flight balance gate.
"""
import logging
import os
from datetime import datetime, timezone

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


def _cfg() -> tuple[str, str, str] | None:
    """Return (url, token, meter_id) if render billing is fully configured, else None."""
    url = os.environ.get("STUDIO_BILLING_URL", "").strip()
    token = os.environ.get("STUDIO_COMMERCE_TOKEN", "").strip()
    meter = os.environ.get("STUDIO_BILLING_METER", "").strip()
    if url and token and meter:
        return url, token, meter
    return None


def count_outputs(history_result: dict | None) -> int:
    """Count media artifacts (images, gifs, audio, ...) a completed render produced."""
    n = 0
    for node_output in (history_result or {}).get("outputs", {}).values():
        if isinstance(node_output, dict):
            for v in node_output.values():
                if isinstance(v, list):
                    n += len(v)
    return n


async def record_render(org_id: str, prompt_id: str, n_outputs: int) -> None:
    """
    Emit ONE metered usage event for a completed render to Commerce meter-events.

    Fire-and-forget with a single retry (the idempotency key makes the retry safe
    against double-counting). Never raises and never blocks: a billing failure
    only logs, a render is never failed for it. No-op unless STUDIO_BILLING_URL,
    STUDIO_COMMERCE_TOKEN and STUDIO_BILLING_METER are all set — so local dev is
    unaffected.
    """
    cfg = _cfg()
    if cfg is None:
        return
    url, token, meter = cfg

    payload = {
        "events": [
            {
                "meterId": meter,
                "userId": org_id or "default",
                "value": max(0, int(n_outputs)),
                "idempotency": prompt_id,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dimensions": {"service": "studio"},
            }
        ]
    }
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(
        total=float(os.environ.get("STUDIO_BILLING_TIMEOUT", "5"))
    )

    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return
                    body = await resp.text()
                    logging.warning(
                        "billing: meter-events returned %s: %s", resp.status, body[:200]
                    )
                    if resp.status < 500:
                        return  # deterministic client error — a retry won't help
        except Exception as e:  # timeout / connection reset — retry once, then drop
            logging.warning(
                "billing: render usage post failed (attempt %d/2): %s", attempt, e
            )
    # Both attempts failed. Drop it — a render is never failed for billing.


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
