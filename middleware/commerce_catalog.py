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

Env:
    STUDIO_CLOUD_API_URL   cloud base (default https://api.hanzo.ai)
    STUDIO_ASSET_BASE_URL  public base for rendered assets (default the S3 bucket the
                           output mirror writes: https://s3.hanzo.ai/hanzo-studio)
    STUDIO_COMMERCE_TOKEN  optional store-scoped service token for the listing write
"""
from __future__ import annotations

import os
import posixpath

import aiohttp
from aiohttp import web

CLOUD_URL = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai").rstrip("/")
ASSET_BASE = os.environ.get("STUDIO_ASSET_BASE_URL", "https://s3.hanzo.ai/hanzo-studio").rstrip("/")
_TIMEOUT = aiohttp.ClientTimeout(total=20)

# The write needs a store-scoped credential. A user bearer usually has none, and the
# upstream says exactly that — which we relay rather than swallow.
_SCOPE_REMEDY = (" — this token has no store scope. Set STUDIO_COMMERCE_TOKEN to the "
                 "store-scoped service token (the same credential cloud's publish edge "
                 "uses), or publish through the CMS lane.")


def asset_url(org: str, rel: str) -> str:
    """The public URL of a library asset, from the ONE key convention
    (``orgs/<org>/output/<rel>``) that content_publish and the output mirror share."""
    key = posixpath.join("orgs", org, "output", (rel or "").lstrip("/"))
    return f"{ASSET_BASE}/{key}"


def _bearer(request) -> str:
    """The caller's own credential, or the configured store token when present. Returns a
    full ``Bearer <tok>`` value, or "" when the caller is anonymous."""
    server = (os.environ.get("STUDIO_COMMERCE_TOKEN") or "").strip()
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

        st, store = await current_store(request, org)
        sid = (store or {}).get("store", {}).get("id") if st == 200 else None
        if not sid:
            return web.json_response({"error": "no store for this org", "upstream": store}, status=st)

        url = asset_url(org, rel)
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
