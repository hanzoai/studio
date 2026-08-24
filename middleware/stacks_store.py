"""Stacks — Procreate-style organizational folders for the Studio gallery.

A Stack groups library assets into a named folder (drag one image onto another,
or multi-select → Stack). Stacks are ONLY an organizational layer: the assets
themselves stay exactly where they are (library.json rows + files), and lineage,
jobs and storage remain the source of truth. Removing an image from a Stack
returns it to the gallery; it is never a delete. Deleting a Stack defaults to
preserving every image.

Backed by ONE per-org SQLite file (stdlib sqlite3, WAL) at ``orgs/<org>/stacks.db``
— WAL + NORMAL + busy_timeout, and every
mutation runs under ``BEGIN IMMEDIATE`` so create / add / move / remove / reorder /
delete are atomic. Tenancy is structural: a different org is a different FILE, so
a stack from another org is unreachable by construction (every route 404s across
orgs). One stack per asset is enforced by ``UNIQUE(asset_id)`` on the membership
table — a membership row exists only while the asset lives in a (non-deleted)
Stack, so "one non-deleted Stack per asset" falls out of the constraint.

The schema is exactly the spec's ``stacks`` + ``stack_assets`` join, plus two
small sidecar tables that keep generation-integration and the per-org setting in
the same tenant-scoped file:

  stack_dest   pending "outputs of this render → this Stack" routing (prefix→id)
  settings     per-org key/value (e.g. auto_stack_batches)

Standard library only — no new dependency.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from aiohttp import web

import folder_paths

_UNTITLED = "Untitled Stack"
_MAX_NAME = 80
_MAX_DESC = 2000
_MAX_ASSET = 512
_MAX_ASSETS = 2000            # a Stack is a folder, not a database — bound it
_DEST_TTL = 6 * 3600.0        # a render lands within minutes; prune stale routing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stacks (
  id              TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  workspace_id    TEXT,
  owner_id        TEXT,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  cover_asset_id  TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  archived_at     INTEGER,
  deleted_at      INTEGER
);
CREATE TABLE IF NOT EXISTS stack_assets (
  id         TEXT PRIMARY KEY,
  stack_id   TEXT NOT NULL,
  asset_id   TEXT NOT NULL UNIQUE,          -- one Stack per asset
  position   INTEGER NOT NULL,
  added_by   TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stack_assets_stack ON stack_assets(stack_id, position);
CREATE TABLE IF NOT EXISTS stack_dest (
  prefix     TEXT PRIMARY KEY,
  stack_id   TEXT NOT NULL,
  added_by   TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_conns: dict[str, sqlite3.Connection] = {}
_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _db_path(org: str) -> Path:
    """``orgs/<org>/stacks.db`` — a per-org sibling of the output tree that holds
    library.json/worklog.json/templates, so a Stack DB is tenant-scoped exactly
    like every other per-org file, by asking the one place that resolves it."""
    return Path(folder_paths.get_org_directory(org)) / "stacks.db"


def _conn(org: str) -> tuple[sqlite3.Connection, threading.Lock]:
    """The org's connection + its write lock, opened once and reused (one durable
    handle per tenant file, like the render queue)."""
    path = str(_db_path(org))
    with _registry_lock:
        c = _conns.get(path)
        if c is None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.executescript(_SCHEMA)
            _conns[path] = c
            _locks[path] = threading.Lock()
        return c, _locks[path]


def _now() -> int:
    return int(time.time())


def _clean_ids(raw) -> list[str]:
    """A client asset-id list → safe, de-duped, order-preserving, bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for x in (raw if isinstance(raw, list) else []):
        if not isinstance(x, str):
            continue
        s = x.strip()[:_MAX_ASSET]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= _MAX_ASSETS:
            break
    return out


def _row(stack: sqlite3.Row) -> dict:
    return {
        "id": stack["id"],
        "organizationId": stack["organization_id"],
        "workspaceId": stack["workspace_id"],
        "ownerId": stack["owner_id"],
        "name": stack["name"],
        "description": stack["description"] or "",
        "coverAssetId": stack["cover_asset_id"],
        "createdAt": stack["created_at"],
        "updatedAt": stack["updated_at"],
        "archivedAt": stack["archived_at"],
    }


# ── membership primitives (all run inside a caller-held BEGIN IMMEDIATE) ─────────
def _detach(c: sqlite3.Connection, asset_id: str, now: int) -> None:
    """Remove an asset from whatever Stack currently holds it, keeping that Stack's
    cover valid — the move-between-Stacks and create-steals-asset invariant. Does
    NOT touch the asset itself (files/library rows are never Stacks' concern)."""
    row = c.execute("SELECT stack_id FROM stack_assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        return
    src = row["stack_id"]
    c.execute("DELETE FROM stack_assets WHERE asset_id=?", (asset_id,))
    cover = c.execute("SELECT cover_asset_id FROM stacks WHERE id=?", (src,)).fetchone()
    if cover and cover["cover_asset_id"] == asset_id:
        nxt = c.execute("SELECT asset_id FROM stack_assets WHERE stack_id=? ORDER BY position LIMIT 1",
                        (src,)).fetchone()
        c.execute("UPDATE stacks SET cover_asset_id=?, updated_at=? WHERE id=?",
                  (nxt["asset_id"] if nxt else None, now, src))
    else:
        c.execute("UPDATE stacks SET updated_at=? WHERE id=?", (now, src))


def _append(c: sqlite3.Connection, stack_id: str, asset_ids: list[str], added_by: str, now: int) -> None:
    """Append assets to a Stack in order, each first detached from its old Stack."""
    nextpos = c.execute("SELECT COALESCE(MAX(position)+1,0) AS n FROM stack_assets WHERE stack_id=?",
                        (stack_id,)).fetchone()["n"]
    for aid in asset_ids:
        _detach(c, aid, now)
        c.execute("INSERT INTO stack_assets(id, stack_id, asset_id, position, added_by, created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (uuid.uuid4().hex, stack_id, aid, nextpos, added_by, now))
        nextpos += 1


def _members(c: sqlite3.Connection, stack_id: str) -> list[dict]:
    rows = c.execute("SELECT asset_id, position, added_by, created_at FROM stack_assets"
                     " WHERE stack_id=? ORDER BY position", (stack_id,)).fetchall()
    return [{"assetId": r["asset_id"], "position": r["position"],
             "addedBy": r["added_by"], "createdAt": r["created_at"]} for r in rows]


# ── public API (each opens its own BEGIN IMMEDIATE; tenancy = the org's file) ────
def create(org: str, name: str, asset_ids, *, owner: str = "", workspace: str | None = None) -> dict:
    name = (name or "").strip()[:_MAX_NAME] or _UNTITLED
    ids = _clean_ids(asset_ids)
    sid = uuid.uuid4().hex
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("INSERT INTO stacks(id, organization_id, workspace_id, owner_id, name,"
                      " description, cover_asset_id, created_at, updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (sid, org, workspace, owner, name, "", ids[0] if ids else None, now, now))
            _append(c, sid, ids, owner, now)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return get(org, sid)


def get(org: str, sid: str) -> dict | None:
    c, _ = _conn(org)
    row = c.execute("SELECT * FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                    (sid, org)).fetchone()
    if row is None:
        return None
    out = _row(row)
    out["assets"] = _members(c, sid)
    out["count"] = len(out["assets"])
    return out


def list_stacks(org: str, *, include_archived: bool = True) -> list[dict]:
    """All of the org's live Stacks, newest-touched first, each with its member
    asset ids (the gallery hides stacked assets from the loose grid and draws the
    layered thumbnail from the cover + first members)."""
    c, _ = _conn(org)
    q = "SELECT * FROM stacks WHERE organization_id=? AND deleted_at IS NULL"
    if not include_archived:
        q += " AND archived_at IS NULL"
    q += " ORDER BY updated_at DESC"
    out = []
    for row in c.execute(q, (org,)).fetchall():
        s = _row(row)
        members = _members(c, row["id"])
        s["assets"] = members
        s["assetIds"] = [m["assetId"] for m in members]
        s["count"] = len(members)
        out.append(s)
    return out


def patch(org: str, sid: str, fields: dict) -> dict | None:
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT id FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                            (sid, org)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            updates: list[tuple[str, object]] = []
            if "name" in fields:
                updates.append(("name", (str(fields.get("name") or "").strip()[:_MAX_NAME]) or _UNTITLED))
            if "description" in fields:
                updates.append(("description", str(fields.get("description") or "")[:_MAX_DESC]))
            if "cover" in fields or "coverAssetId" in fields:
                cover = str(fields.get("cover") or fields.get("coverAssetId") or "").strip()[:_MAX_ASSET]
                # A cover must be a member — else the card would show a ghost.
                member = c.execute("SELECT 1 FROM stack_assets WHERE stack_id=? AND asset_id=?",
                                   (sid, cover)).fetchone()
                if cover and member is None:
                    raise ValueError("cover must be an asset in this Stack")
                updates.append(("cover_asset_id", cover or None))
            if "archived" in fields:
                updates.append(("archived_at", now if fields.get("archived") else None))
            updates.append(("updated_at", now))
            cols = ", ".join(f"{col}=?" for col, _ in updates)
            args = [val for _, val in updates] + [sid, org]
            c.execute(f"UPDATE stacks SET {cols} WHERE id=? AND organization_id=?", args)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return get(org, sid)


def add_assets(org: str, sid: str, asset_ids, *, added_by: str = "") -> dict | None:
    ids = _clean_ids(asset_ids)
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT cover_asset_id FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                            (sid, org)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            _append(c, sid, ids, added_by, now)
            if row["cover_asset_id"] is None and ids:
                c.execute("UPDATE stacks SET cover_asset_id=? WHERE id=?", (ids[0], sid))
            c.execute("UPDATE stacks SET updated_at=? WHERE id=?", (now, sid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return get(org, sid)


def remove_assets(org: str, sid: str, asset_ids) -> dict | None:
    """Remove assets from a Stack — they return to the main gallery, never deleted.
    Positions compact; a removed cover falls back to the new first member."""
    ids = _clean_ids(asset_ids)
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT cover_asset_id FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                            (sid, org)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            for aid in ids:
                c.execute("DELETE FROM stack_assets WHERE stack_id=? AND asset_id=?", (sid, aid))
            # compact positions to keep them contiguous and stable
            rest = c.execute("SELECT id FROM stack_assets WHERE stack_id=? ORDER BY position",
                             (sid,)).fetchall()
            for i, r in enumerate(rest):
                c.execute("UPDATE stack_assets SET position=? WHERE id=?", (i, r["id"]))
            cover = row["cover_asset_id"]
            if cover in ids:
                nxt = c.execute("SELECT asset_id FROM stack_assets WHERE stack_id=? ORDER BY position LIMIT 1",
                                (sid,)).fetchone()
                c.execute("UPDATE stacks SET cover_asset_id=? WHERE id=?",
                          (nxt["asset_id"] if nxt else None, sid))
            c.execute("UPDATE stacks SET updated_at=? WHERE id=?", (now, sid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return get(org, sid)


def reorder(org: str, sid: str, asset_ids) -> dict | None:
    """Persist a new order. The given ids must be exactly the Stack's current
    membership (same set) — a partial/foreign list is rejected so the order can
    never silently drop or invent a member."""
    ids = _clean_ids(asset_ids)
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT id FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                            (sid, org)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            cur = {r["asset_id"] for r in c.execute("SELECT asset_id FROM stack_assets WHERE stack_id=?", (sid,)).fetchall()}
            if set(ids) != cur:
                raise ValueError("order must list exactly the Stack's assets")
            for i, aid in enumerate(ids):
                c.execute("UPDATE stack_assets SET position=? WHERE stack_id=? AND asset_id=?", (i, sid, aid))
            c.execute("UPDATE stacks SET updated_at=? WHERE id=?", (now, sid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return get(org, sid)


def _dissolve(org: str, sid: str) -> list[str] | None:
    """Soft-delete the container and free its members (membership rows removed so
    the assets are unstacked and one-per-asset stays true). Returns the freed asset
    ids, or None if the Stack isn't the caller-org's live Stack. Backs both Unstack
    and Delete-Stack."""
    now = _now()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT id FROM stacks WHERE id=? AND organization_id=? AND deleted_at IS NULL",
                            (sid, org)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            members = [r["asset_id"] for r in
                       c.execute("SELECT asset_id FROM stack_assets WHERE stack_id=? ORDER BY position", (sid,)).fetchall()]
            c.execute("DELETE FROM stack_assets WHERE stack_id=?", (sid,))
            c.execute("UPDATE stacks SET deleted_at=?, updated_at=? WHERE id=?", (now, now, sid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return members


def unstack(org: str, sid: str) -> list[str] | None:
    """Delete the container, preserve every asset to the main gallery."""
    return _dissolve(org, sid)


def delete(org: str, sid: str, mode: str = "only") -> dict | None:
    """Delete a Stack. ``only`` (default) returns its images to the gallery;
    ``images`` also asks the caller to soft-delete them. Files are never touched.
    Returns ``{ok, mode, assetIds}`` (assetIds = the freed/soft-deletable assets),
    or None across orgs / already gone."""
    freed = _dissolve(org, sid)
    if freed is None:
        return None
    return {"ok": True, "mode": ("images" if mode == "images" else "only"), "assetIds": freed}


def stack_of(org: str, asset_id: str) -> str | None:
    c, _ = _conn(org)
    row = c.execute("SELECT stack_id FROM stack_assets WHERE asset_id=?", (asset_id,)).fetchone()
    return row["stack_id"] if row else None


# ── generation integration: route a render's outputs into a chosen Stack ─────────
def route_prefix(org: str, output_prefix: str, sid: str, *, added_by: str = "") -> None:
    """Remember "every output of the render at ``output_prefix`` goes into Stack
    ``sid``". Recorded at dispatch; consumed by :func:`absorb` when each output
    lands (so ALL images of a multi-output job land in the same Stack). Stale
    routing is pruned — a render lands within minutes."""
    if not output_prefix or not sid:
        return
    now = time.time()
    c, lock = _conn(org)
    with lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM stack_dest WHERE created_at < ?", (now - _DEST_TTL,))
            c.execute("INSERT INTO stack_dest(prefix, stack_id, added_by, created_at) VALUES(?,?,?,?)"
                      " ON CONFLICT(prefix) DO UPDATE SET stack_id=excluded.stack_id,"
                      " added_by=excluded.added_by, created_at=excluded.created_at",
                      (output_prefix, sid, added_by, now))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


_COUNTER = re.compile(r"_\d+_\.\w+$")


def absorb(org: str, rel: str, *, added_by: str = "") -> str | None:
    """A just-ingested asset ``rel`` → the Stack its render was routed to, if any.
    Matches a landed ``<prefix>_<counter>_.png`` back to its ``output_prefix`` (and
    the ingested-path case where the stored path IS the prefix). Idempotent via the
    membership UNIQUE. Returns the Stack id it joined, or None."""
    if not rel:
        return None
    c, _ = _conn(org)
    cand = c.execute("SELECT stack_id FROM stack_dest WHERE prefix=?", (rel,)).fetchone()
    if cand is None:
        prefix = _COUNTER.sub("", rel)
        if prefix != rel:
            cand = c.execute("SELECT stack_id FROM stack_dest WHERE prefix=?", (prefix,)).fetchone()
    if cand is None:
        return None
    sid = cand["stack_id"]
    if add_assets(org, sid, [rel], added_by=added_by or "generation") is None:
        return None
    return sid


# ── per-org settings (auto-stack batch generations) ─────────────────────────────
def get_setting(org: str, key: str, default=None):
    c, _ = _conn(org)
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def set_setting(org: str, key: str, value) -> None:
    c, lock = _conn(org)
    with lock:
        c.execute("INSERT INTO settings(key, value) VALUES(?,?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


# ── HTTP routes: /v1/stacks… (house style: /v1, never /api). Dependencies are
# INJECTED (org / owner / workspace resolvers + the library soft-deleter) so this
# module stays decoupled from studio_home — no import cycle, one stacks concern. ──
def add_stacks_routes(routes: web.RouteTableDef, server, *, org_of, owner_of, workspace_of,
                      soft_delete_assets) -> None:

    async def _json(request):
        try:
            return await request.json()
        except Exception:
            return None

    @routes.get("/v1/stacks")
    async def list_route(request: web.Request):
        org = org_of(request)
        return web.json_response({"org": org, "stacks": list_stacks(org)})

    @routes.post("/v1/stacks")
    async def create_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        stack = create(org_of(request), body.get("name"), body.get("assetIds"),
                       owner=owner_of(request), workspace=workspace_of(request))
        return web.json_response(stack)

    # /v1/stacks/settings MUST precede /v1/stacks/{id} — else "settings" reads as an id.
    @routes.get("/v1/stacks/settings")
    async def get_settings_route(request: web.Request):
        org = org_of(request)
        return web.json_response({"org": org, "autoStack": bool(get_setting(org, "auto_stack_batches", False))})

    @routes.patch("/v1/stacks/settings")
    async def set_settings_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        org = org_of(request)
        if "autoStack" in body:
            set_setting(org, "auto_stack_batches", bool(body.get("autoStack")))
        return web.json_response({"org": org, "autoStack": bool(get_setting(org, "auto_stack_batches", False))})

    @routes.get("/v1/stacks/{id}")
    async def get_route(request: web.Request):
        stack = get(org_of(request), request.match_info["id"])
        if stack is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(stack)

    @routes.patch("/v1/stacks/{id}")
    async def patch_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            stack = patch(org_of(request), request.match_info["id"], body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if stack is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(stack)

    @routes.delete("/v1/stacks/{id}")
    async def delete_route(request: web.Request):
        org = org_of(request)
        mode = "images" if request.query.get("mode") == "images" else "only"
        result = delete(org, request.match_info["id"], mode)
        if result is None:
            return web.json_response({"error": "not found"}, status=404)
        if mode == "images" and result["assetIds"]:
            soft_delete_assets(org, result["assetIds"])
        return web.json_response(result)

    @routes.post("/v1/stacks/{id}/assets")
    async def add_assets_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        stack = add_assets(org_of(request), request.match_info["id"], body.get("assetIds"),
                           added_by=owner_of(request))
        if stack is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(stack)

    @routes.delete("/v1/stacks/{id}/assets")
    async def remove_assets_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        stack = remove_assets(org_of(request), request.match_info["id"], body.get("assetIds"))
        if stack is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(stack)

    @routes.patch("/v1/stacks/{id}/order")
    async def order_route(request: web.Request):
        body = await _json(request)
        if body is None:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            stack = reorder(org_of(request), request.match_info["id"], body.get("assetIds"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if stack is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(stack)

    @routes.post("/v1/stacks/{id}/unstack")
    async def unstack_route(request: web.Request):
        freed = unstack(org_of(request), request.match_info["id"])
        if freed is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True, "assetIds": freed})
