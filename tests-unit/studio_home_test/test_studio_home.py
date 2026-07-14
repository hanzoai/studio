"""Unit tests for the Studio Home surface (middleware/studio_home.py)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import studio_home as sh


def test_reseed_randomizes_every_sampler_seed():
    graph = {
        "a": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 30}},
        "b": {"class_type": "KSamplerAdvanced", "inputs": {"noise_seed": 7}},
        "c": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
    }
    out = sh._reseed(json.loads(json.dumps(graph)))
    assert out["a"]["inputs"]["steps"] == 30
    # 2**31 space: a collision with the old seed twice over is not a thing.
    assert (out["a"]["inputs"]["seed"], out["b"]["inputs"]["noise_seed"]) != (7, 7)


def test_fix_graph_keeps_recipe_invariants():
    g = sh._fix_graph("ref.png", "make the straps thicker", "fixes/test")
    ks = g["13"]["inputs"]
    assert ks["cfg"] == 4.0 and ks["steps"] == 30            # proven floor, not the weak 3.0
    assert g["12"]["class_type"] == "CFGNorm"
    assert g["8"]["inputs"]["reference_latents_method"] == "index_timestep_zero"
    assert g["6"]["inputs"]["image1"] == ["2", 0]            # scaled ref, never raw
    assert "straps thicker" in g["6"]["inputs"]["prompt"]
    assert g["15"]["inputs"]["filename_prefix"] == "fixes/test"


def test_safe_asset_blocks_traversal(tmp_path):
    root = tmp_path / "out"
    (root / "designs").mkdir(parents=True)
    (root / "designs" / "a.png").write_bytes(b"x")
    (tmp_path / "secret.txt").write_text("no")
    assert sh._safe_asset(root, "designs/a.png") is not None
    assert sh._safe_asset(root, "../secret.txt") is None
    assert sh._safe_asset(root, "designs/missing.png") is None


def test_status_vocab_matches_library_lifecycle():
    assert sh._STATUSES == ("draft", "approved", "queued", "published")


@pytest.mark.asyncio
async def test_home_redirect_only_hits_root_html(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setenv("STUDIO_HOME_DEFAULT", "1")

    async def handler(_):
        return web.Response(text="spa")

    # root + html accept -> redirect to /studio
    req = make_mocked_request("GET", "/", headers={"Accept": "text/html"})
    with pytest.raises(web.HTTPFound) as e:
        await sh.home_redirect(req, handler)
    assert e.value.location == "/studio"

    # advanced escape hatch and non-root paths pass through untouched
    for path, accept in [("/?advanced=1", "text/html"), ("/assets/x.js", "*/*"), ("/", "application/json")]:
        req = make_mocked_request("GET", path, headers={"Accept": accept})
        resp = await sh.home_redirect(req, handler)
        assert resp.text == "spa"

    # env off (local dev / worker pods): root is untouched
    monkeypatch.delenv("STUDIO_HOME_DEFAULT")
    req = make_mocked_request("GET", "/", headers={"Accept": "text/html"})
    resp = await sh.home_redirect(req, handler)
    assert resp.text == "spa"
