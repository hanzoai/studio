"""
Pre-flight balance gate for POST /prompt (on with STUDIO_ENABLE_BILLING and
STUDIO_BILLING_CHECK_BALANCE). Reads the render org's balance from cloud's commerce
API as Studio's own IAM application (middleware.commerce).

Posture: fail open. A render is refused (402) only when a balance read FOR ITS ORG
shows nothing available. A balance that cannot be read for the org — no service
identity, IAM or cloud unreachable or non-200, or a token that does not act for the
org — allows the render and logs why.
"""
import logging

from middleware import commerce

# The gate's answer when the org's balance was not read: allow; -1 marks "unread".
_UNREAD = (True, -1)


async def check_balance(org_id: str) -> tuple[bool, int]:
    """(allowed, available_cents) for a render billed to org_id."""
    try:
        available = (await commerce.balance(org_id))["available"]
    except commerce.Mismatch as e:
        logging.warning("billing: token does not act for org %s (%s); allowing", org_id, e)
        return _UNREAD
    except Exception as e:
        logging.warning("billing: balance unread for org %s (%s); allowing", org_id, e)
        return _UNREAD
    return available > 0, available
