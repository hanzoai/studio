"""Studio 3D lane — Hunyuan3D-2.1 image-to-mesh (.glb) over the shared library/queue/
flywheel. Mirrors the video-lane tests: the proven graph builder, the dispatch route
(org from the token, unique job tag, one required reference), and .glb ingest/serve."""

import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from middleware import studio_home as sh
from middleware import worklog as wl


class _Req:
    def __init__(self, *, body=None, query=None, org="acme"):
        self._body = body
        self.query = query or {}
        self.headers = {}
        self._user = {"org_id": org, "sub": f"u@{org}"} if org else None
        self.cookies = {}
        self.content_length = None
    def get(self, key, default=None):
        return self._user if key == "iam_user" else default
    async def json(self):
        return self._body if self._body is not None else {}


class _FakeQueue:
    def get_tasks_remaining(self):
        return 0


class _FakeServer:
    port = 1
    def __init__(self):
        self.prompt_queue = _FakeQueue()


@pytest.fixture
def orgaware(tmp_path, monkeypatch):
    def outd(o="default"):
        d = tmp_path / "orgs" / o / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    def ind(o="default"):
        d = tmp_path / "orgs" / o / "input"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", outd)
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", ind)
    monkeypatch.setattr(sh.folder_paths, "get_output_directory", lambda: str(tmp_path))
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory", outd)
    return outd, ind


def _handler(server, method, path, monkeypatch):
    monkeypatch.setattr(sh.stacks_store, "add_stacks_routes", lambda *a, **k: None)
    routes = web.RouteTableDef()
    sh.add_studio_home_routes(routes, server)
    for r in routes:
        if getattr(r, "method", None) == method and getattr(r, "path", None) == path:
            return r.handler
    raise AssertionError(f"no route {method} {path}")


# ── 1. the proven Hunyuan3D graph ──────────────────────────────────────────────────
def test_model3d_graph_is_the_proven_hunyuan3d():
    g = sh._model3d_graph("ref.png", "model3d/x_ab12cd", seed=7)
    cls = {k: v["class_type"] for k, v in g.items()}
    # the ONE bundled checkpoint (not separate loaders — that produced noise)
    assert g["1"]["class_type"] == "ImageOnlyCheckpointLoader"
    assert g["1"]["inputs"]["ckpt_name"] == sh._HY3D_CKPT
    # the flow-matching wrapper WITHOUT which the sampler yields fragments
    assert "ModelSamplingAuraFlow" in cls.values()
    assert g["5"]["inputs"]["shift"] == 1.0 and g["5"]["inputs"]["model"] == ["1", 0]
    # image → clip-vision → conditioning; latent is EmptyLatentHunyuan3Dv2(4096)
    assert g["2"]["inputs"]["image"] == "ref.png"
    assert g["6"]["inputs"]["resolution"] == 4096
    assert g["7"]["inputs"]["seed"] == 7 and g["7"]["inputs"]["cfg"] == 5.0
    # ends at SaveGLB with the unique prefix
    assert g["10"]["class_type"] == "SaveGLB"
    assert g["10"]["inputs"]["filename_prefix"] == "model3d/x_ab12cd"
    assert "VAEDecodeHunyuan3D" in cls.values() and "VoxelToMesh" in cls.values()


# ── 2. dispatch + route (org from token, unique tag, one required reference) ────────
@pytest.mark.asyncio
async def test_dispatch_model3d_route(orgaware, monkeypatch):
    outd, _ = orgaware
    root = Path(outd("acme"))
    root.joinpath("library.json").write_text('{"assets":[]}')
    (root / "designs").mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGB", (8, 8)).save(root / "designs" / "hero.png", "PNG")
    captured = {}
    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "pid-3d", ""
    monkeypatch.setattr(sh, "_submit", fake_queue)
    model3d = _handler(_FakeServer(), "POST", "/v1/library/model3d", monkeypatch)

    # a library image → 3D; 'org' in the body is ignored (token org wins)
    resp = await model3d(_Req(body={"path": "designs/hero.png", "org": "evil"}, org="acme"))
    assert resp.status == 200
    import re
    out = json.loads(resp.text)
    assert re.match(r"^model3d/.+_[0-9a-f]{6}$", out["output_prefix"])   # unique job tag
    rows = wl.load("acme")
    assert len(rows) == 1 and rows[0]["kind"] == "model3d"
    assert wl.load("evil") == []
    assert any(n.get("class_type") == "SaveGLB" for n in captured["graph"].values())

    # NO reference → 400 (there is no text-to-3D)
    r2 = await model3d(_Req(body={}, org="acme"))
    assert r2.status == 400
    # both a path and an upload → 400
    r3 = await model3d(_Req(body={"path": "a.png", "upload": "u.png"}, org="acme"))
    assert r3.status == 400


# ── 3. .glb ingest + serve ─────────────────────────────────────────────────────────
def test_glb_is_recognized_as_model3d():
    assert ".glb" in sh._UPLOAD_EXT
    assert sh._CTYPE[".glb"] == "model/gltf-binary"
    assert ".glb" in sh._MODEL3D_EXT and ".glb" in sh._SAVE_EXT
