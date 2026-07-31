"""Commerce ⇄ Studio — the ONE lane between a render and the product catalog.

Studio already publishes a finished render INTO the CMS (``content_publish`` →
``POST /v1/framework/Asset``). That lane is about a *record*. This module is the other
direction and the other object: the **storefront listing** karma.style serves at runtime,
and the catalog Studio needs to READ so a person can choose what they are dressing.

Two directions, both real, neither inventing a surface:

  READ   ``GET /v1/commerce/catalog``  → the org's product catalog (cloud's public read)
         ``GET /v1/commerce/store``    → the org's default store (``/v1/store/current``)
         ``GET /v1/commerce/listing/{slug}`` → one listing as the storefront sees it

  WRITE  ``POST /v1/commerce/publish`` {path, slug} → upsert that listing's
         ``headerImage`` to the render's URL — the SAME edge cloud's own
         ``StorefrontPublish`` writes (``PUT /v1/store/:id/listing/:slug`` with
         ``{slug, headerImage:{type,url,alt}}``), so a render published from Studio and
         one published through the CMS lane land identically. One shape, one convention.

The asset URL is built with ``content_publish.object_key`` — byte-identical to the key
the CMS record uses and to where Studio's own output mirror lands, so the listing, the
record and the bytes can never disagree.

Auth: the caller's own bearer, forwarded. A store write needs a store-scoped credential;
when the caller's token lacks that scope the upstream says so (403 "Token doesn't support
this scope") and we surface it VERBATIM rather than pretending it published. A
server-side ``STUDIO_COMMERCE_TOKEN`` is used when configured — that is the credential
cloud's own publish edge uses for this write.

``headerImage.url`` is PERSISTED on the listing and then fetched by an ANONYMOUS browser
every time the storefront renders. So "did it publish" and "does it resolve" are the same
question, and the only credential that answers it is no credential at all: before writing,
this module reads the URL exactly as the storefront will. A URL that is not publicly
readable is REFUSED with the observed status and the fix — never persisted, because a
listing pointing at an unreadable asset is a permanent silent breakage nobody sees until
a customer does.

Env:
    STUDIO_CLOUD_API_URL   cloud base (default https://api.hanzo.ai)
    STUDIO_ASSET_BASE_URL  PUBLIC read surface for rendered assets. Default
                           https://assets.hanzo.ai — hanzoai/static in front of the
                           ``hanzo-studio`` bucket the output mirror writes, so the key
                           convention is unchanged (``orgs/<org>/output/<rel>`` is the
                           bucket key AND the URL path). NEVER s3.hanzo.ai: that is the
                           credentialed S3 API and denies every anonymous read (403), so
                           a listing built on it can never render.
    STUDIO_STORE_TOKEN     store-scoped service token for the listing write. Its OWN
                           variable on purpose: STUDIO_COMMERCE_TOKEN is the BILLING
                           credential and is a different token with different scope
                           (verified in prod — they hash differently and the billing one
                           is refused by the store). One credential, one purpose; the
                           chat outage came from exactly this kind of overload.
"""
from __future__ import annotations

import os
import posixpath

import aiohttp
from aiohttp import web

CLOUD_URL = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai").rstrip("/")
ASSET_BASE = os.environ.get("STUDIO_ASSET_BASE_URL", "https://assets.hanzo.ai").rstrip("/")
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)

# The write needs a store-scoped credential. A user bearer usually has none, and the
# upstream says exactly that — which we relay rather than swallow.
_SCOPE_REMEDY = (" — this token has no store scope. Set STUDIO_STORE_TOKEN to the "
                 "store-scoped service token (the same credential cloud's publish edge "
                 "uses: cloud-api-secrets/commerceToken), or publish through the CMS lane.")


def asset_url(org: str, rel: str) -> str:
    """The public URL of a library asset, from the ONE key convention
    (``orgs/<org>/output/<rel>``) that content_publish and the output mirror share."""
    key = posixpath.join("orgs", org, "output", (rel or "").lstrip("/"))
    return f"{ASSET_BASE}/{key}"


async def public_read(url: str) -> tuple[int, str]:
    """Read ``url`` the way the storefront will: ANONYMOUSLY. No Authorization header,
    ever — a credential would prove nothing about what a customer's browser sees.
    Returns (status, reason); status 0 means the host could not be reached."""
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as s:
            async with s.head(url, allow_redirects=True) as r:
                return r.status, r.reason or ""
    except Exception as e:  # noqa: BLE001 — the reason is the useful part
        return 0, str(e)


def _why_not_public(code: int, reason: str) -> str:
    """Why an anonymous read failed, and the one thing that fixes it."""
    if code in (401, 403):
        return (f"refused anonymously ({code} {reason}) — {ASSET_BASE} is not a public "
                "read surface. Point STUDIO_ASSET_BASE_URL at one (hanzoai/static in "
                "front of the hanzo-studio bucket); s3.hanzo.ai is the credentialed S3 "
                "API and denies every anonymous read")
    if code == 404:
        # A 404 has TWO causes that need opposite actions, and telling them apart is the
        # difference between "wait 30s" and "wire a host". If the base itself does not
        # serve (its root 404s too), no render will EVER appear there and "retry in a
        # moment" would loop forever — verified: assets.hanzo.ai currently has no
        # ingress and no static deploy, so it 404s every key including the root.
        return (f"not found ({code}) — either the output mirror has not copied this "
                f"render up yet (it polls every ~30s; publish again in a moment), or "
                f"{ASSET_BASE} does not serve this bucket at all. Check the base "
                f"itself: if {ASSET_BASE}/ also 404s, no asset will ever resolve there "
                "— point STUDIO_ASSET_BASE_URL at a public read surface for the "
                "hanzo-studio bucket (hanzoai/static + an ingress route)")
    if code == 0:
        return f"could not be reached: {reason}"
    return f"answered {code} {reason}"


def _bearer(request) -> str:
    """The caller's own credential, or the configured store token when present. Returns a
    full ``Bearer <tok>`` value, or "" when the caller is anonymous."""
    server = (os.environ.get("STUDIO_STORE_TOKEN") or "").strip()
    if server:
        return server if server[:7].lower() == "bearer " else "Bearer " + server
    from middleware.gpu_dispatch import _bearer as caller_bearer
    return caller_bearer(request) or ""


async def _call(request, method: str, path: str, org: str, body=None):
    """One upstream call. Returns (status, json_or_text). Never raises — a transport
    failure is reported as 503 with its reason, so a caller always learns the truth."""
    headers = {"Content-Type": "application/json"}
    tok = _bearer(request)
    if tok:
        headers["Authorization"] = tok
    if org:
        headers["X-Org-Id"] = org
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.request(method, f"{CLOUD_URL}{path}", headers=headers, json=body) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, {"error": (await r.text())[:300]}
    except Exception as e:  # noqa: BLE001 — the reason is the useful part
        return 503, {"error": f"could not reach the commerce API: {e}"}


async def current_store(request, org: str) -> tuple[int, dict]:
    return await _call(request, "GET", "/v1/store/current", org)


def add_commerce_routes(routes: web.RouteTableDef, org_of) -> None:
    """Register the commerce lane. ``org_of(request)`` is injected (the same tenancy
    resolver every other Studio module receives) so this module stays decoupled."""

    @routes.get("/v1/commerce/catalog")
    async def get_catalog(request: web.Request):
        """The org's product catalog, read INTO Studio — so a person can see what
        products exist before choosing what a render is for."""
        status, data = await _call(request, "GET", "/v1/commerce/catalog", org_of(request))
        return web.json_response(data, status=status)

    @routes.get("/v1/commerce/store")
    async def get_store(request: web.Request):
        """The org's default store (id + currencies) — the id every listing write needs."""
        status, data = await current_store(request, org_of(request))
        return web.json_response(data, status=status)

    @routes.get("/v1/commerce/listing/{slug}")
    async def get_listing(request: web.Request):
        """One listing exactly as the storefront reads it — so Studio can show what is
        live for a design before replacing it."""
        org = org_of(request)
        st, store = await current_store(request, org)
        sid = (store or {}).get("store", {}).get("id") if st == 200 else None
        if not sid:
            return web.json_response({"error": "no store for this org", "upstream": store}, status=st)
        slug = request.match_info["slug"]
        status, data = await _call(request, "GET", f"/v1/store/{sid}/listing/{slug}", org)
        return web.json_response(data, status=status)

    @routes.post("/v1/commerce/publish")
    async def publish_listing(request: web.Request):
        """Publish a library render straight to the storefront: upsert the listing's
        ``headerImage`` to that asset. Body ``{path, slug?, alt?}`` — ``slug`` defaults to
        the asset's design folder, matching the design==slug join the CMS lane uses."""
        org = org_of(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        rel = str(body.get("path") or "").strip().lstrip("/")
        if not rel:
            return web.json_response({"error": "path is required (a library asset)"}, status=400)
        slug = str(body.get("slug") or "").strip() or posixpath.dirname(rel).split("/")[-1]
        if not slug:
            return web.json_response(
                {"error": "slug is required — this asset is not inside a design folder"}, status=400)

        # The asset is the part Studio owns, so it is checked FIRST — and by the only
        # test that means anything: an anonymous read, exactly what the storefront does.
        url = asset_url(org, rel)
        code, reason = await public_read(url)
        if not 200 <= code < 300:
            return web.json_response(
                {"error": f"this asset is not publicly readable — {_why_not_public(code, reason)}",
                 "url": url, "asset_status": code, "slug": slug}, status=409)

        st, store = await current_store(request, org)
        sid = (store or {}).get("store", {}).get("id") if st == 200 else None
        if not sid:
            return web.json_response({"error": "no store for this org", "upstream": store}, status=st)

        payload = {"slug": slug,
                   "headerImage": {"type": "image", "url": url,
                                   "alt": str(body.get("alt") or slug)}}
        status, data = await _call(request, "PUT", f"/v1/store/{sid}/listing/{slug}", org, payload)
        if status in (401, 403):
            msg = (data or {}).get("error")
            msg = msg.get("message") if isinstance(msg, dict) else msg
            return web.json_response(
                {"error": f"{msg or 'refused by the store'}{_SCOPE_REMEDY}",
                 "store": sid, "slug": slug, "url": url}, status=status)
        if not 200 <= status < 300:
            return web.json_response({"error": "listing upsert failed", "status": status,
                                      "upstream": data, "slug": slug}, status=status)
        return web.json_response({"ok": True, "store": sid, "slug": slug, "url": url,
                                  "listing": data})
