"""
Cloud's one commerce API, called as Studio's own IAM application.

Studio holds no commerce token. It mints its own application token with the OAuth2
client_credentials grant — the SAME IAM app that signs users in — and sends it as a
Bearer on every billing call, selecting the payer org with ``X-Org-Id``:

    POST {iam}/v1/iam/oauth/token        Basic id:secret, grant_type=client_credentials
    GET  {commerce}/v1/billing/balance   Bearer <app token>, X-Org-Id: <org>

The token is cached and replaced 60s before it expires. A selection the token does not
carry falls back to the app's OWN org, so every balance is checked against the org it
was asked for (``account`` is the org or starts with "<org>/"); an answer for any other
account raises :class:`Mismatch` and is never returned.

Env:
    STUDIO_IAM_URL            IAM base (via --iam-url; default https://hanzo.id)
    STUDIO_IAM_CLIENT_ID      the IAM application (default hanzo-studio)
    STUDIO_IAM_CLIENT_SECRET  its secret (KMS); unset = no service identity
    STUDIO_COMMERCE_URL       commerce API base (default https://api.hanzo.ai)
"""
import asyncio
import os
import time

import aiohttp

from middleware.session import _cfg

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MARGIN = 60  # seconds before expiry at which the cached token is replaced
_clock = time.monotonic

_token = {"value": "", "until": 0.0}
_lock = asyncio.Lock()


class Error(Exception):
    """IAM or cloud gave no usable answer."""


class Mismatch(Error):
    """Cloud answered for another account: the token does not act for the org."""


async def token(session: aiohttp.ClientSession) -> str:
    """Studio's application access token, minted by client_credentials and cached."""
    async with _lock:
        if _token["value"] and _clock() < _token["until"]:
            return _token["value"]
        client_id = os.environ.get("STUDIO_IAM_CLIENT_ID", "hanzo-studio")
        secret = os.environ.get("STUDIO_IAM_CLIENT_SECRET", "")
        if not secret:
            raise Error("STUDIO_IAM_CLIENT_SECRET is unset: no service identity")
        iam = _cfg("iam_url", "STUDIO_IAM_URL", "https://hanzo.id").rstrip("/")
        async with session.post(
            f"{iam}/v1/iam/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=aiohttp.BasicAuth(client_id, secret),
        ) as r:
            if r.status != 200:
                raise Error(f"IAM token {r.status}: {(await r.text())[:200]}")
            body = await r.json(content_type=None)
        access = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(access, str) or not access:
            raise Error("IAM token response carries no access_token")
        ttl = body.get("expires_in")
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            ttl = 0  # no stated lifetime: use once, mint again next call
        _token.update(value=access, until=_clock() + ttl - _MARGIN)
        return access


async def balance(org: str) -> dict:
    """The org's balance as {balance, holds, available, account}, in cents.

    Raises Mismatch when cloud answered for another account, Error when IAM or cloud
    gave no usable answer."""
    base = os.environ.get("STUDIO_COMMERCE_URL", "https://api.hanzo.ai").rstrip("/")
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        bearer = await token(s)
        async with s.get(
            f"{base}/v1/billing/balance",
            headers={"Authorization": f"Bearer {bearer}", "X-Org-Id": org},
        ) as r:
            if r.status == 401:
                _token.update(value="", until=0.0)  # revoked or rotated: mint again next call
            if r.status != 200:
                raise Error(f"balance {r.status}: {(await r.text())[:200]}")
            body = await r.json(content_type=None)
    if not isinstance(body, dict):
        raise Error("balance response is not an object")
    account = body.get("account")
    if not isinstance(account, str) or not (account == org or account.startswith(org + "/")):
        raise Mismatch(f"balance answered for account {account!r}, not org {org!r}")
    available = body.get("available")
    if not isinstance(available, int) or isinstance(available, bool):
        raise Error(f"balance has no integer available: {available!r}")
    return body
