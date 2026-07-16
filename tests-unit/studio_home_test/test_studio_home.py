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


class _FakeQueue:
    def __init__(self, running=None, pending=None):
        self.running = running or []
        self.pending = pending or []
        self.deleted = []

    def get_current_queue_volatile(self):
        return (self.running, self.pending)

    def delete_queue_item(self, fn):
        before = len(self.pending)
        self.pending = [it for it in self.pending if not fn(it)]
        self.deleted.append(before - len(self.pending))


class _FakeServer:
    port = 1

    def __init__(self, **kw):
        self.prompt_queue = _FakeQueue(**kw)


def _orgaware(tmp_path, monkeypatch):
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
    return outd, ind


def test_cancel_marks_cancelled_and_removes_gpu_row_org_scoped(tmp_path, monkeypatch):
    outd, _ = _orgaware(tmp_path, monkeypatch)
    (Path(outd("acme")) / "library.json").write_text('{"assets":[]}')
    sh.worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                      output_prefix="fixes/x", pid="p1", lane="gpu")
    (Path(outd("acme")) / "render_jobs.json").write_text(json.dumps([{"id": "p1", "prefix": "x"}]))
    srv = _FakeServer()
    assert sh._owns_job(srv, "acme", "p1")
    assert not sh._owns_job(srv, "globex", "p1")            # cross-tenant: not owned
    assert sh._cancel_job(srv, "acme", "p1") == "gpu"
    assert sh.worklog.load("acme")[0]["status"] == "cancelled"
    assert json.loads((Path(outd("acme")) / "render_jobs.json").read_text()) == []


def test_cancel_deletes_local_pending_only_when_org_matches(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    mine = (1, "p1", {}, {"org_id": "acme"}, [])
    theirs = (2, "p2", {}, {"org_id": "globex"}, [])
    srv = _FakeServer(pending=[mine, theirs])
    sh.worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                      output_prefix="fixes/x", pid="p1")
    assert sh._cancel_job(srv, "acme", "p1") == "local"
    assert [it[1] for it in srv.prompt_queue.pending] == ["p2"]   # only mine deleted


@pytest.mark.asyncio
async def test_amend_atomic_swap_keeps_original_refs_and_adds_new(tmp_path, monkeypatch):
    outd, ind = _orgaware(tmp_path, monkeypatch)
    (Path(outd("acme")) / "library.json").write_text('{"assets":[]}')
    (Path(ind("acme")) / "fix_src_1.png").write_bytes(b"\x89PNG")   # original ref, still present
    (Path(ind("acme")) / "drop_9.png").write_bytes(b"\x89PNG")      # new upload
    sh.worklog.record("acme", kind="fix", prompt="brighten", refs=["fix_src_1.png"],
                      uploads=[], parents=["a.png"], output_prefix="fixes/a", pid="p1", lane="local")
    captured = {}
    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "p2"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    out = await sh.dispatch_amend(None, _FakeServer(), "acme", "p1", "brighten a lot more",
                                  paths=[], uploads=["drop_9.png"])
    assert out["amended_from"] == "p1" and out["prompt_id"] == "p2" and out["refs"] == 2
    rows = {r["id"]: r for r in sh.worklog.load("acme")}
    assert rows["p1"]["status"] == "cancelled"                      # original swapped out
    assert rows["p2"]["status"] == "queued" and rows["p2"]["prompt"] == "brighten a lot more"
    loads = [n["inputs"]["image"] for n in captured["graph"].values() if n.get("class_type") == "LoadImage"]
    assert "fix_src_1.png" in loads and "drop_9.png" in loads       # original kept + new added


@pytest.mark.asyncio
async def test_amend_unknown_job_is_not_owned(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    with pytest.raises(sh.NotOwned):
        await sh.dispatch_amend(None, _FakeServer(), "acme", "ghost", "change it")


@pytest.mark.asyncio
async def test_amend_terminal_job_rejected(tmp_path, monkeypatch):
    outd, _ = _orgaware(tmp_path, monkeypatch)
    sh.worklog.record("acme", kind="fix", prompt="x", refs=["r.png"], uploads=[], parents=[],
                      output_prefix="fixes/x", pid="p1")
    sh.worklog.set_status("acme", "p1", "done")
    with pytest.raises(sh.DispatchError):
        await sh.dispatch_amend(None, _FakeServer(), "acme", "p1", "too late")


def test_prompt_error_message_extracts_real_reason():
    # model-less pod: /prompt returns a validation error — the user must SEE it.
    body = {"error": {"type": "prompt_outputs_failed_validation",
                      "message": "Prompt outputs failed validation",
                      "details": "UNETLoader: unet_name 'qwen-image-edit-2511.safetensors' not in []"}}
    msg = sh._prompt_error_message(body)
    assert "Prompt outputs failed validation" in msg and "not in []" in msg
    assert sh._prompt_error_message({}) and "silent" not in sh._prompt_error_message({}).lower()
    assert sh._prompt_error_message({"error": "no GPU worker"}) == "no GPU worker"


@pytest.mark.asyncio
async def test_text_only_fix_dispatches_single_image(tmp_path, monkeypatch):
    """REGRESSION LOCK: click Fix, type text, no references → the single-image
    /v1/library/fix path (base library asset staged, proven Qwen edit graph)."""
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
    captured = {}
    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "pid-fix"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    out = await sh.dispatch_fix(None, None, org, "a.png", "make the straps thicker")
    assert out["ok"] and out["prompt_id"] == "pid-fix"
    g = captured["graph"]
    assert g["1"]["class_type"] == "LoadImage" and g["1"]["inputs"]["image"].startswith("fix_src_")
    assert g["15"]["class_type"] == "SaveImage" and "straps thicker" in g["6"]["inputs"]["prompt"]


@pytest.mark.asyncio
async def test_fix_accepts_uploaded_base_image(tmp_path, monkeypatch):
    """A freshly uploaded photo can be the fix base (upload=) without landing it in
    the library first — the '+ add a photo' single-ref path."""
    org = "acme"
    in_dir = tmp_path / "orgs" / org / "input"
    in_dir.mkdir(parents=True)
    (in_dir / "shot_1.png").write_bytes(b"\x89PNG-up")
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", lambda o=None: str(in_dir))
    captured = {}
    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "pid-up"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    out = await sh.dispatch_fix(None, None, org, "", "brighten it", upload="shot_1.png")
    assert out["ok"] and captured["graph"]["1"]["inputs"]["image"] == "shot_1.png"
    with pytest.raises(sh.DispatchError):
        await sh.dispatch_fix(None, None, org, "", "brighten it", upload="../etc/passwd")


@pytest.mark.asyncio
async def test_queue_prompt_surfaces_downstream_error(tmp_path, monkeypatch):
    """A non-200 from the self-POST /prompt (model-less pod) becomes a DispatchError
    carrying the REAL reason — never a raw 502 the dialog swallows into 'Failed:'."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    class Srv:
        port = None
    srv = Srv()
    routes = web.RouteTableDef()

    @routes.post("/prompt")
    async def prompt(request):
        return web.json_response({"error": {"message": "Prompt outputs failed validation",
                                            "details": "UNETLoader: unet_name '…' not in []"}}, status=400)

    async def edit(request):
        graph = (await request.json())["graph"]
        try:
            pid = await sh._queue_prompt(request, srv, graph)
            return web.json_response({"prompt_id": pid})
        except sh.DispatchError as e:
            return web.json_response({"error": str(e)}, status=400)
    routes.post("/edit")(edit)

    app = web.Application()
    app.add_routes(routes)
    ts = TestServer(app)
    await ts.start_server()
    srv.port = ts.port
    client = TestClient(ts)
    await client.start_server()
    r = await client.post("/edit", json={"graph": {"1": {"class_type": "SaveImage", "inputs": {}}}})
    body = await r.json()
    assert r.status == 400 and "error" in body
    assert "failed validation" in body["error"] and "not in []" in body["error"]  # real reason, shown
    await client.close()


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
