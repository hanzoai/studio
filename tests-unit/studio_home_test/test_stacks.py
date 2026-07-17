"""Stacks — the Procreate-style gallery folders.

Covers the per-org SQLite store (middleware/stacks_store.py) and its wiring into
the Studio home routes: every REST route, transactional membership, tenancy
isolation, one-stack-per-asset, order persistence, and delete/unstack semantics
(remove is never delete; delete defaults to preserving; delete-images soft-deletes
in the library), plus the generation-integration routing and auto-stack setting.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import stacks_store as st
from middleware import studio_home as sh


@pytest.fixture
def org(tmp_path, monkeypatch):
    """A tmp per-org tree + a fresh connection cache — the SAME folder_paths seam
    production tenancy uses, so a Stack DB lands at ``orgs/<org>/stacks.db``."""
    def outd(o=None):
        d = tmp_path / "orgs" / (o or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def ind(o=None):
        d = tmp_path / "orgs" / (o or "default") / "input"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", outd)
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", ind)
    monkeypatch.setattr(sh.folder_paths, "get_output_directory", lambda: str(tmp_path))
    for c in st._conns.values():
        try:
            c.close()
        except Exception:
            pass
    st._conns.clear()
    st._locks.clear()
    yield "acme"


# ── store: the file lives where tenancy expects it ──────────────────────────────
def test_db_path_is_org_rooted(org, tmp_path):
    st.create(org, "x", [])
    assert (tmp_path / "orgs" / "acme" / "stacks.db").is_file()


# ── create: drag A onto B = a Stack with BOTH, nothing copied/merged/deleted ─────
def test_create_from_two_assets_holds_both_cover_first(org):
    s = st.create(org, "Fashion", ["a.png", "b.png"])
    assert s["name"] == "Fashion" and s["count"] == 2
    assert [m["assetId"] for m in s["assets"]] == ["a.png", "b.png"]
    assert s["coverAssetId"] == "a.png"             # default cover = first image
    # reloading from the DB yields the same membership (nothing copied/merged)
    got = st.get(org, s["id"])
    assert [m["assetId"] for m in got["assets"]] == ["a.png", "b.png"]


def test_create_defaults_untitled_and_dedupes(org):
    s = st.create(org, "   ", ["a.png", "a.png", "b.png"])
    assert s["name"] == "Untitled Stack"
    assert [m["assetId"] for m in s["assets"]] == ["a.png", "b.png"]


def test_single_image_stack_allowed(org):
    s = st.create(org, "solo", ["only.png"])
    assert s["count"] == 1 and st.get(org, s["id"])["count"] == 1


def test_empty_stack_allowed_for_generation_destination(org):
    s = st.create(org, "New", [])
    assert s["count"] == 0 and s["coverAssetId"] is None


# ── one stack per asset: adding to a second Stack MOVES it ───────────────────────
def test_one_stack_per_asset_move_on_readd(org):
    a = st.create(org, "A", ["x.png", "y.png"])
    b = st.create(org, "B", ["z.png"])
    st.add_assets(org, b["id"], ["x.png"])          # steal x from A
    assert [m["assetId"] for m in st.get(org, a["id"])["assets"]] == ["y.png"]
    assert [m["assetId"] for m in st.get(org, b["id"])["assets"]] == ["z.png", "x.png"]
    assert st.stack_of(org, "x.png") == b["id"]


def test_moving_the_cover_out_repoints_source_cover(org):
    a = st.create(org, "A", ["cover.png", "second.png"])
    assert a["coverAssetId"] == "cover.png"
    b = st.create(org, "B", [])
    st.add_assets(org, b["id"], ["cover.png"])      # cover leaves A
    assert st.get(org, a["id"])["coverAssetId"] == "second.png"


# ── add / remove: remove returns to gallery, never deletes ───────────────────────
def test_remove_returns_asset_and_repoints_cover(org):
    s = st.create(org, "S", ["a.png", "b.png", "c.png"])
    st.remove_assets(org, s["id"], ["a.png"])       # a.png was the cover
    got = st.get(org, s["id"])
    assert [m["assetId"] for m in got["assets"]] == ["b.png", "c.png"]
    assert [m["position"] for m in got["assets"]] == [0, 1]   # positions compact
    assert got["coverAssetId"] == "b.png"
    assert st.stack_of(org, "a.png") is None        # free again, not deleted


# ── reorder: order persists and is validated against membership ──────────────────
def test_reorder_persists_and_survives_reload(org):
    s = st.create(org, "S", ["a.png", "b.png", "c.png"])
    st.reorder(org, s["id"], ["c.png", "a.png", "b.png"])
    assert [m["assetId"] for m in st.get(org, s["id"])["assets"]] == ["c.png", "a.png", "b.png"]


def test_reorder_rejects_wrong_set(org):
    s = st.create(org, "S", ["a.png", "b.png"])
    with pytest.raises(ValueError):
        st.reorder(org, s["id"], ["a.png"])                 # missing a member
    with pytest.raises(ValueError):
        st.reorder(org, s["id"], ["a.png", "b.png", "c.png"])  # foreign member


# ── rename / describe / cover / archive ─────────────────────────────────────────
def test_patch_name_description_cover(org):
    s = st.create(org, "old", ["a.png", "b.png"])
    p = st.patch(org, s["id"], {"name": "new", "description": "desc", "cover": "b.png"})
    assert p["name"] == "new" and p["description"] == "desc" and p["coverAssetId"] == "b.png"


def test_cover_must_be_a_member(org):
    s = st.create(org, "S", ["a.png"])
    with pytest.raises(ValueError):
        st.patch(org, s["id"], {"cover": "not-in-stack.png"})


def test_archive_hides_from_default_list(org):
    s = st.create(org, "S", ["a.png"])
    st.patch(org, s["id"], {"archived": True})
    assert st.get(org, s["id"])["archivedAt"] is not None
    assert s["id"] not in [x["id"] for x in st.list_stacks(org, include_archived=False)]
    assert s["id"] in [x["id"] for x in st.list_stacks(org, include_archived=True)]


# ── unstack: container gone, EVERY asset preserved ──────────────────────────────
def test_unstack_preserves_all_assets(org):
    s = st.create(org, "S", ["a.png", "b.png", "c.png"])
    freed = st.unstack(org, s["id"])
    assert freed == ["a.png", "b.png", "c.png"]
    assert st.get(org, s["id"]) is None                 # container dissolved
    assert all(st.stack_of(org, a) is None for a in freed)


# ── delete: defaults to preserving images; images-mode soft-deletes them ─────────
def test_delete_only_returns_images(org):
    s = st.create(org, "S", ["a.png", "b.png"])
    out = st.delete(org, s["id"], "only")
    assert out["mode"] == "only" and out["assetIds"] == ["a.png", "b.png"]
    assert st.get(org, s["id"]) is None
    assert st.stack_of(org, "a.png") is None


def test_delete_images_reports_assets_for_soft_delete(org):
    s = st.create(org, "S", ["a.png", "b.png"])
    out = st.delete(org, s["id"], "images")
    assert out["mode"] == "images" and set(out["assetIds"]) == {"a.png", "b.png"}


# ── generation integration: route a prefix, absorb the landed outputs ───────────
def test_route_and_absorb_assigns_all_outputs(org):
    s = st.create(org, "Batch", [])
    st.route_prefix(org, "renders/foo", s["id"])
    # SaveImage counter form + a second output of the same job both join
    assert st.absorb(org, "renders/foo_00001_.png") == s["id"]
    assert st.absorb(org, "renders/foo_00002_.png") == s["id"]
    got = st.get(org, s["id"])
    assert [m["assetId"] for m in got["assets"]] == ["renders/foo_00001_.png", "renders/foo_00002_.png"]
    assert got["coverAssetId"] == "renders/foo_00001_.png"


def test_absorb_ingested_path_and_miss(org):
    s = st.create(org, "S", [])
    st.route_prefix(org, "composes/bar", s["id"])
    assert st.absorb(org, "composes/bar") == s["id"]        # ingested-path (no counter)
    assert st.absorb(org, "renders/unrelated_00001_.png") is None


# ── per-org settings (auto-stack batch generations) ─────────────────────────────
def test_settings_roundtrip(org):
    assert st.get_setting(org, "auto_stack_batches", False) is False
    st.set_setting(org, "auto_stack_batches", True)
    assert st.get_setting(org, "auto_stack_batches", False) is True


def test_dest_stack_auto_creates_for_batch_when_enabled(org):
    # explicit id wins
    assert sh._dest_stack(org, "sid-123", prompt="x", multi=True, owner="", workspace=None) == "sid-123"
    # off → None even for a batch
    assert sh._dest_stack(org, None, prompt="a red hat", multi=True, owner="", workspace=None) is None
    st.set_setting(org, "auto_stack_batches", True)
    # on + single output → still None (auto-stack is for batches)
    assert sh._dest_stack(org, None, prompt="x", multi=False, owner="", workspace=None) is None
    # on + batch → a new '<short prompt> — <date>' Stack
    sid = sh._dest_stack(org, None, prompt="a red hat on a model", multi=True, owner="u1", workspace="proj")
    got = st.get(org, sid)
    assert got is not None and got["name"].startswith("a red hat on a") and got["ownerId"] == "u1"
    assert got["workspaceId"] == "proj"


def test_index_upload_absorbs_landed_output_into_routed_stack(org, tmp_path):
    """The real ingest point: a render routed to a Stack, then its output landing
    via _index_upload, joins that Stack — the ONE membership-assignment hook (and it
    still returns `created` for the quality gate)."""
    root = Path(sh.folder_paths.get_org_output_directory(org))
    (root / "library.json").write_text(json.dumps({"assets": []}))
    s = st.create(org, "Gen", [])
    st.route_prefix(org, "renders/gen", s["id"])
    created = sh._index_upload(org, root, "renders/gen_00001_.png",
                               design=None, kind="render", role=None, node="upload")
    assert created is True                                   # gate contract preserved
    assert [m["assetId"] for m in st.get(org, s["id"])["assets"]] == ["renders/gen_00001_.png"]


def test_soft_delete_assets_marks_library_deleted(org, tmp_path):
    root = Path(sh.folder_paths.get_org_output_directory(org))
    (root / "library.json").write_text(json.dumps({"assets": [
        {"path": "a.png", "status": "draft"}, {"path": "b.png", "status": "approved"}]}))
    sh._soft_delete_assets(org, ["a.png"])
    lib = json.loads((root / "library.json").read_text())
    by = {a["path"]: a["status"] for a in lib["assets"]}
    assert by == {"a.png": "deleted", "b.png": "approved"}   # only the named one, files never unlinked
    assert (root / "library.json").is_file()


# ══ route surface ═══════════════════════════════════════════════════════════════
class _FakeServer:
    port = 1


async def _client(monkeypatch, sub="u1"):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    @web.middleware
    async def fake_auth(request, handler):
        o = request.headers.get("X-Org")
        if o:
            request["iam_user"] = {"org_id": o, "orgs": [o], "sub": sub}
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    routes = web.RouteTableDef()
    sh.add_studio_home_routes(routes, _FakeServer())
    app.add_routes(routes)
    ts = TestServer(app)
    await ts.start_server()
    client = TestClient(ts)
    await client.start_server()
    return client


async def _mk(client, org, name, ids):
    r = await client.post("/v1/stacks", json={"name": name, "assetIds": ids}, headers={"X-Org": org})
    assert r.status == 200
    return await r.json()


@pytest.mark.asyncio
async def test_routes_full_lifecycle(org, monkeypatch):
    client = await _client(monkeypatch)
    # create
    s = await _mk(client, "acme", "Fashion", ["a.png", "b.png"])
    assert s["count"] == 2 and s["ownerId"] == "u1"
    sid = s["id"]
    # list
    lst = await (await client.get("/v1/stacks", headers={"X-Org": "acme"})).json()
    assert lst["org"] == "acme" and [x["id"] for x in lst["stacks"]] == [sid]
    # get
    assert (await (await client.get(f"/v1/stacks/{sid}", headers={"X-Org": "acme"})).json())["name"] == "Fashion"
    # add
    r = await client.post(f"/v1/stacks/{sid}/assets", json={"assetIds": ["c.png"]}, headers={"X-Org": "acme"})
    assert [m["assetId"] for m in (await r.json())["assets"]] == ["a.png", "b.png", "c.png"]
    # reorder (persists)
    r = await client.patch(f"/v1/stacks/{sid}/order", json={"assetIds": ["c.png", "b.png", "a.png"]}, headers={"X-Org": "acme"})
    assert [m["assetId"] for m in (await r.json())["assets"]] == ["c.png", "b.png", "a.png"]
    reloaded = await (await client.get(f"/v1/stacks/{sid}", headers={"X-Org": "acme"})).json()
    assert [m["assetId"] for m in reloaded["assets"]] == ["c.png", "b.png", "a.png"]
    # rename + cover
    r = await client.patch(f"/v1/stacks/{sid}", json={"name": "Looks", "cover": "b.png"}, headers={"X-Org": "acme"})
    body = await r.json()
    assert body["name"] == "Looks" and body["coverAssetId"] == "b.png"
    # remove (returns to gallery)
    r = await client.delete(f"/v1/stacks/{sid}/assets", json={"assetIds": ["c.png"]}, headers={"X-Org": "acme"})
    assert [m["assetId"] for m in (await r.json())["assets"]] == ["b.png", "a.png"]
    await client.close()


@pytest.mark.asyncio
async def test_routes_unstack_and_delete_default_preserves(org, monkeypatch):
    client = await _client(monkeypatch)
    s = await _mk(client, "acme", "S", ["a.png", "b.png"])
    r = await client.post(f"/v1/stacks/{s['id']}/unstack", headers={"X-Org": "acme"})
    assert (await r.json())["assetIds"] == ["a.png", "b.png"]
    assert (await client.get(f"/v1/stacks/{s['id']}", headers={"X-Org": "acme"})).status == 404
    # delete default (mode=only) preserves images
    s2 = await _mk(client, "acme", "S2", ["x.png"])
    r = await client.delete(f"/v1/stacks/{s2['id']}", headers={"X-Org": "acme"})
    j = await r.json()
    assert j["mode"] == "only" and j["assetIds"] == ["x.png"]
    await client.close()


@pytest.mark.asyncio
async def test_routes_delete_images_soft_deletes_library(org, monkeypatch):
    root = Path(sh.folder_paths.get_org_output_directory("acme"))
    (root / "library.json").write_text(json.dumps({"assets": [{"path": "a.png", "status": "draft"}]}))
    client = await _client(monkeypatch)
    s = await _mk(client, "acme", "S", ["a.png"])
    r = await client.delete(f"/v1/stacks/{s['id']}?mode=images", headers={"X-Org": "acme"})
    assert (await r.json())["mode"] == "images"
    lib = json.loads((root / "library.json").read_text())
    assert lib["assets"][0]["status"] == "deleted"           # soft-deleted, file kept
    await client.close()


@pytest.mark.asyncio
async def test_routes_tenancy_cross_org_404(org, monkeypatch):
    client = await _client(monkeypatch)
    s = await _mk(client, "acme", "Secret", ["a.png"])
    sid = s["id"]
    # globex sees an empty list and cannot read/patch/delete acme's Stack
    assert (await (await client.get("/v1/stacks", headers={"X-Org": "globex"})).json())["stacks"] == []
    assert (await client.get(f"/v1/stacks/{sid}", headers={"X-Org": "globex"})).status == 404
    assert (await client.patch(f"/v1/stacks/{sid}", json={"name": "x"}, headers={"X-Org": "globex"})).status == 404
    assert (await client.post(f"/v1/stacks/{sid}/assets", json={"assetIds": ["z.png"]}, headers={"X-Org": "globex"})).status == 404
    assert (await client.delete(f"/v1/stacks/{sid}", headers={"X-Org": "globex"})).status == 404
    assert (await client.post(f"/v1/stacks/{sid}/unstack", headers={"X-Org": "globex"})).status == 404
    # acme still intact
    assert (await client.get(f"/v1/stacks/{sid}", headers={"X-Org": "acme"})).status == 200
    await client.close()


@pytest.mark.asyncio
async def test_routes_validation_and_settings(org, monkeypatch):
    client = await _client(monkeypatch)
    # bad cover → 400
    s = await _mk(client, "acme", "S", ["a.png"])
    assert (await client.patch(f"/v1/stacks/{s['id']}", json={"cover": "ghost.png"}, headers={"X-Org": "acme"})).status == 400
    # bad reorder → 400
    assert (await client.patch(f"/v1/stacks/{s['id']}/order", json={"assetIds": ["a.png", "b.png"]}, headers={"X-Org": "acme"})).status == 400
    # settings roundtrip
    assert (await (await client.get("/v1/stacks/settings", headers={"X-Org": "acme"})).json())["autoStack"] is False
    r = await client.patch("/v1/stacks/settings", json={"autoStack": True}, headers={"X-Org": "acme"})
    assert (await r.json())["autoStack"] is True
    # "settings" is NOT read as a stack id
    assert (await client.get("/v1/stacks/settings", headers={"X-Org": "acme"})).status == 200
    await client.close()
