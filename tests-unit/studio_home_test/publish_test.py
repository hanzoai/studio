"""Library → "Publish to CMS": pushing a gallery asset into the Hanzo content lane.

Two concerns, one route:

  * backend  — POST /v1/library/publish composes the EXISTING writer
               (middleware/content_publish) over the EXISTING library + work log: one
               draft Asset, carrying the graph the output embeds and the prompt of the
               row that produced it. Refusals come back whole — a 403 from a role-less
               principal is a 403 with its remedy, an unknown asset is a 404, a signed-
               out caller is a 401, a switched-off instance is a 503. Never a fabricated
               success. Tenancy is the caller's org, server-resolved.
  * frontend — the shipped gallery gains that action (card · bulk bar · viewer) with a
               label distinct from its OWN local "Publish" (curation state), and the
               context panel renders real provenance for a library asset.
"""
import json
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import content_publish
from middleware import studio_home as sh
from middleware import worklog

_HTML = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()

TOKEN = "the-users-iam-access-token"
GRAPH = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-edit-2511.safetensors"}},
         "2": {"class_type": "KSampler", "inputs": {"steps": 20, "cfg": 4.0, "seed": 7,
                                                    "sampler_name": "euler", "scheduler": "simple"}},
         "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fixes/x_ab12cd"}}}


class Lane:
    """A stand-in for hanzoai/cloud's framework surface (module 'marketing')."""

    def __init__(self, status: int = 201, body: dict | None = None):
        self.assets: list[dict] = []
        self.status, self.body = status, body or {}

    async def _asset(self, request):
        if self.status not in (200, 201):
            return web.json_response(self.body, status=self.status)
        doc = await request.json()
        doc["_auth"] = request.headers.get("Authorization")
        self.assets.append(doc)
        return web.json_response({**doc, "status": "draft"}, status=201)


@pytest.fixture
def org(tmp_path, monkeypatch):
    """A tmp per-org tree through the SAME folder_paths seam production tenancy uses,
    holding one landed PNG that is in the library and owned by a work-log row."""
    def outd(o=None):
        d = tmp_path / "orgs" / (o or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", outd)
    monkeypatch.setattr(sh.folder_paths, "get_output_directory", lambda: str(tmp_path))
    monkeypatch.setattr(sh, "_embedded", lambda p: {"prompt": json.dumps(GRAPH)})
    monkeypatch.setenv("STUDIO_CONTENT_PUBLISH", "1")
    root = Path(outd("acme"))
    (root / "fixes").mkdir(parents=True, exist_ok=True)
    (root / "fixes" / "x_ab12cd_00001_.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "library.json").write_text(json.dumps({"assets": [
        {"path": "fixes/x_ab12cd_00001_.png", "design": "valentina", "kind": "lifestyle",
         "role": "life1", "status": "draft", "tags": ["valentina", "life1"]}]}))
    worklog.record("acme", kind="fix", prompt="brighten the straps", refs=[], uploads=[],
                   parents=["base.png"], output_prefix="fixes/x_ab12cd",
                   params={"steps": 20}, pid="pid-1", status="queued")
    worklog.mark_finished("acme", "pid-1", int(time.time()))
    sh._PROV_CACHE.clear()
    return "acme"


class _FakeServer:
    port = 1


async def _client(lane: Lane | None, monkeypatch, token: str | None = TOKEN):
    """The real studio-home routes behind a fake-auth middleware that stamps the same
    request keys production's IAM middleware does, with `lane` standing in for the cloud."""
    if lane is not None:
        lane_app = web.Application()
        lane_app.router.add_post("/v1/framework/Asset", lane._asset)
        lane_srv = TestServer(lane_app)
        await lane_srv.start_server()
        monkeypatch.setattr(content_publish, "CLOUD_URL", str(lane_srv.make_url("")).rstrip("/"))

    @web.middleware
    async def fake_auth(request, handler):
        o = request.headers.get("X-Org")
        if o:
            request["iam_user"] = {"org_id": o, "orgs": [o], "sub": "u1"}
        if token:
            request["iam_token"] = token
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


async def _publish(client, path="fixes/x_ab12cd_00001_.png", org="acme"):
    return await client.post("/v1/library/publish", json={"path": path}, headers={"X-Org": org})


# ── the asset that lands is the provenance record ───────────────────────────────────
@pytest.mark.asyncio
async def test_publishes_one_draft_asset_carrying_its_provenance(org, monkeypatch):
    lane = Lane()
    r = await _publish(await _client(lane, monkeypatch))
    assert r.status == 200 and (await r.json())["ok"] is True
    assert len(lane.assets) == 1
    a = lane.assets[0]
    # the bytes Studio already persisted — this writes a record, never an upload
    assert a["file"] == "orgs/acme/output/fixes/x_ab12cd_00001_.png"
    assert a["render_params"] == GRAPH                    # the graph reproduces it
    assert a["generator"] == "qwen-image-edit-2511.safetensors"
    assert a["source_prompt_id"] == "pid-1"               # the row that produced it
    assert a["prompt"] == "brighten the straps"
    assert a["design"] == "valentina" and a["kind"] == "lifestyle" and a["role"] == "life1"
    assert a["tags"] == "valentina,life1"                 # library list → the DocType's string
    assert "status" not in {k: v for k, v in a.items() if k != "_auth"}   # the lane defaults it to draft
    assert a["_auth"] == f"Bearer {TOKEN}"                # written AS the caller, once-prefixed


# ── refusals arrive whole ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_forbidden_is_surfaced_as_403_with_its_remedy(org, monkeypatch):
    """The known cloud-side gap: the render principal holds no Content Editor role. The
    UI must be able to say so — status, upstream body and the fix, never a swallowed OK."""
    lane = Lane(403, {"error": "permission denied: create on Asset"})
    r = await _publish(await _client(lane, monkeypatch))
    assert r.status == 403
    msg = (await r.json())["error"]
    assert "permission denied: create on Asset" in msg
    assert content_publish.FORBIDDEN_REMEDY in msg


@pytest.mark.asyncio
async def test_unknown_asset_is_404(org, monkeypatch):
    r = await _publish(await _client(Lane(), monkeypatch), path="../../etc/passwd")
    assert r.status == 404
    r = await _publish(await _client(Lane(), monkeypatch), path="fixes/never_rendered.png")
    assert r.status == 404


@pytest.mark.asyncio
async def test_signed_out_caller_gets_an_honest_401_not_a_fake_ok(org, monkeypatch):
    lane = Lane()
    r = await _publish(await _client(lane, monkeypatch, token=None))
    assert r.status == 401 and "sign in" in (await r.json())["error"].lower()
    assert lane.assets == []


@pytest.mark.asyncio
async def test_disabled_instance_says_so_rather_than_pretending(org, monkeypatch):
    monkeypatch.delenv("STUDIO_CONTENT_PUBLISH")
    lane = Lane()
    r = await _publish(await _client(lane, monkeypatch))
    assert r.status == 503 and "switched off" in (await r.json())["error"]
    assert lane.assets == []


@pytest.mark.asyncio
async def test_another_orgs_asset_is_never_publishable(org, monkeypatch):
    """Org comes from the verified claim only; a sibling tenant's path is not in the
    caller's library, so it 404s — its existence is never confirmed, nothing is written."""
    lane = Lane()
    r = await _publish(await _client(lane, monkeypatch), org="other")
    assert r.status == 404 and lane.assets == []


# ── provenance is a JOIN, not a new store ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_lineage_returns_the_producing_job(org, monkeypatch):
    client = await _client(None, monkeypatch)
    r = await client.get("/v1/worklog/lineage?path=fixes/x_ab12cd_00001_.png",
                         headers={"X-Org": "acme"})
    lin = await r.json()
    assert r.status == 200 and lin["job"]["id"] == "pid-1"
    assert lin["job"]["prompt"] == "brighten the straps"
    assert lin["job"]["kind"] == "fix" and lin["job"]["events"]
    assert [a["path"] for a in lin["ancestors"]] == ["base.png"]


def test_provenance_is_one_distiller_over_the_embedded_graph(org, monkeypatch):
    prov = sh._provenance(Path(sh.folder_paths.get_org_output_directory("acme"))
                          / "fixes" / "x_ab12cd_00001_.png")
    # _graph_params — the SAME distiller the dispatch record uses — plus what only a
    # finished artifact carries: the saver's prefix, the model and the graph size.
    assert prov["steps"] == 20 and prov["cfg"] == 4.0 and prov["seed"] == 7
    assert prov["sampler"] == "euler" and prov["scheduler"] == "simple"
    assert prov["workflow"] == "fixes/x_ab12cd"
    assert prov["model"] == "qwen-image-edit-2511.safetensors" and prov["nodes"] == 3


# ── the served page ─────────────────────────────────────────────────────────────────
def test_publish_to_cms_is_a_distinct_action_from_local_publish():
    # the local curation button/route are untouched…
    assert "onclick=\"setSt(${i},'published')\">Publish<" in _HTML
    assert "onclick=\"bulk('published')\">Publish<" in _HTML
    # …and the cross-system push has its own label, its own route, and reaches the
    # asset from the card, the bulk bar and the full-size viewer.
    assert "const CMSLBL='☁ Publish to CMS'" in _HTML
    assert "'/v1/library/publish'" in _HTML
    assert "publishCMS('${pe}',this)" in _HTML and 'id="bulkcms"' in _HTML and 'id="zrcms"' in _HTML


def test_publish_failure_is_shown_verbatim_never_a_toast():
    # a 403 must read as a 403 with its message, in something that stays on screen
    assert "function pubFailed(" in _HTML and 'id="pubdlg"' in _HTML
    assert "'Publish to CMS failed — '+title" in _HTML
    assert "errMsg(j)||('HTTP '+r.status)" in _HTML
    # a draft is what lands — the UI must not imply the image is live on a storefront
    assert "Sent to CMS as a draft" in _HTML


def test_library_asset_context_renders_real_provenance():
    # openLineage hydrates from lineage.job (prompt/refs/params/trail), and the panel
    # gained a Provenance block with a link to the full graph.
    assert "lin.job||{}" in _HTML
    assert "const provOf=" in _HTML and "provOf(it.output)" in _HTML
    assert '<div class="ctxlabel">Provenance</div>' in _HTML
    assert "/v1/workflow?id=" in _HTML


def test_there_is_still_exactly_one_gallery():
    # the QC gallery is #agrid, fed by the ONE /v1/library fetch. Publish + provenance
    # EXTEND it — they add no second grid and no second source of assets.
    assert _HTML.count('id="agrid"') == 1
    assert _HTML.count("await fetch('/v1/library')") == 1
