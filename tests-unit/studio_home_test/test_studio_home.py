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
    assert sh._STATUSES == ("draft", "approved", "queued", "published", "deleted")


def test_resolve_uploads_keeps_only_safe_existing_images(tmp_path, monkeypatch):
    in_dir = tmp_path / "orgs" / "acme" / "input"
    in_dir.mkdir(parents=True)
    (in_dir / "ref.png").write_bytes(b"\x89PNG")
    (in_dir / "shot.jpg").write_bytes(b"jpg")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", lambda org=None: str(in_dir))
    out = sh._resolve_uploads("acme", [
        "ref.png",                 # kept
        "sub/dir/shot.jpg",        # basename kept (upload names are flat)
        "ref.png",                 # de-duped
        "../secret.txt",           # traversal + non-image -> dropped
        "missing.png",             # absent -> dropped
        "notes.txt",               # non-image -> dropped
        42,                        # non-string -> dropped
    ])
    assert out == ["ref.png", "shot.jpg"]


@pytest.mark.asyncio
async def test_compose_accepts_uploaded_references(tmp_path, monkeypatch):
    """A base library asset + one uploaded image is a valid 2-reference compose:
    the upload is named directly (already in the input dir), the library asset is
    staged, and both reach the graph — no lower bound violation."""
    org = "acme"
    root = tmp_path / "orgs" / org / "output"
    root.mkdir(parents=True)
    (root / "library.json").write_text(json.dumps({"assets": [{"path": "a.png"}]}))
    (root / "a.png").write_bytes(b"\x89PNG-base")
    in_dir = tmp_path / "orgs" / org / "input"
    in_dir.mkdir(parents=True)
    (in_dir / "up_0001_.png").write_bytes(b"\x89PNG-up")

    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", lambda o=None: str(in_dir))
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    monkeypatch.setattr(sh.folder_paths, "get_output_directory", lambda: str(tmp_path))

    captured = {}
    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "pid-1"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)

    out = await sh.dispatch_compose(None, None, org, ["a.png"], "merge them", uploads=["up_0001_.png"])
    assert out["ok"] and out["refs"] == 2
    load_names = [n["inputs"]["image"] for n in captured["graph"].values()
                 if n["class_type"] == "LoadImage"]
    assert "up_0001_.png" in load_names            # upload referenced by its own name
    assert any(n.startswith("compose_src_") for n in load_names)  # library asset staged


@pytest.mark.asyncio
async def test_compose_rejects_single_reference(tmp_path, monkeypatch):
    org = "acme"
    root = tmp_path / "orgs" / org / "output"
    root.mkdir(parents=True)
    (root / "library.json").write_text(json.dumps({"assets": [{"path": "a.png"}]}))
    (root / "a.png").write_bytes(b"\x89PNG")
    in_dir = tmp_path / "orgs" / org / "input"
    in_dir.mkdir(parents=True)
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", lambda o=None: str(in_dir))
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    monkeypatch.setattr(sh.folder_paths, "get_output_directory", lambda: str(tmp_path))
    with pytest.raises(sh.DispatchError):
        await sh.dispatch_compose(None, None, org, ["a.png"], "just one", uploads=[])


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
