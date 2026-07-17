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
    assert sh._STATUSES == ("draft", "approved", "queued", "published", "deleted", "flagged")


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


# ── Shared shell: editor index injection + templates + workflow + wallet ─────────
async def _client(server, org_middleware=True):
    """A real aiohttp app with the studio-home routes and a fake-auth middleware that
    stamps request['iam_user'] from an X-Org header — so route tests exercise the
    SAME server-side org scoping (_org_of) production uses."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    @web.middleware
    async def fake_auth(request, handler):
        o = request.headers.get("X-Org")
        if o:
            request["iam_user"] = {"org_id": o, "orgs": [o]}
        return await handler(request)

    app = web.Application(middlewares=[fake_auth] if org_middleware else [])
    routes = web.RouteTableDef()
    sh.add_studio_home_routes(routes, server)
    app.add_routes(routes)
    ts = TestServer(app)
    await ts.start_server()
    client = TestClient(ts)
    await client.start_server()
    return client


def test_editor_index_injects_shell_once(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<html><head></head><body><div id=app></div></body></html>")
    resp = sh.editor_index(str(root))
    assert resp.headers["Cache-Control"] == "no-cache"
    assert '<link rel="stylesheet" href="/studio/shell.css"></head>' in resp.text
    assert '<script src="/studio/shell.js" defer></script></body>' in resp.text
    # idempotent: an index that already carries the shell is served unchanged
    (root / "index.html").write_text(resp.text)
    again = sh.editor_index(str(root))
    assert again.text.count("/studio/shell.js") == 1


def test_template_api_prompt_shapes():
    api = {"9": {"class_type": "KSampler", "inputs": {}}, "15": {"class_type": "SaveImage", "inputs": {}}}
    assert sh._template_api_prompt({"output": api, "workflow": {"nodes": []}}) == api   # editor save
    assert sh._template_api_prompt(api) == api                                          # bare API prompt
    assert sh._template_api_prompt({"nodes": [{"id": 1}], "last_node_id": 1}) is None   # UI-only, not runnable
    assert sh._template_api_prompt({}) is None and sh._template_api_prompt("x") is None
    assert sh._template_slug("Karma — Ghost Mannequin!") == "karma-ghost-mannequin"


@pytest.mark.asyncio
async def test_templates_crud_and_tenant_isolation(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    graph = {"output": {"15": {"class_type": "SaveImage", "inputs": {"filename_prefix": "t/x"}}}}
    r = await client.post("/v1/templates", json={"name": "Ghost Mannequin", "graph": graph}, headers={"X-Org": "acme"})
    assert r.status == 200 and (await r.json())["slug"] == "ghost-mannequin"
    # acme sees it (renderable — the graph carries an API prompt), fetches it by slug
    r = await client.get("/v1/templates", headers={"X-Org": "acme"})
    body = await r.json()
    assert body["org"] == "acme" and len(body["templates"]) == 1
    assert body["templates"][0]["name"] == "Ghost Mannequin" and body["templates"][0]["renderable"] is True
    r = await client.get("/v1/templates/ghost-mannequin", headers={"X-Org": "acme"})
    assert (await r.json())["graph"] == graph
    # tenant isolation: globex's list is empty and it can't read acme's template
    assert (await (await client.get("/v1/templates", headers={"X-Org": "globex"})).json())["templates"] == []
    assert (await client.get("/v1/templates/ghost-mannequin", headers={"X-Org": "globex"})).status == 404
    # validation
    assert (await client.post("/v1/templates", json={"name": "", "graph": graph}, headers={"X-Org": "acme"})).status == 400
    assert (await client.post("/v1/templates", json={"name": "x", "graph": {}}, headers={"X-Org": "acme"})).status == 400
    await client.close()


@pytest.mark.asyncio
async def test_template_render_dispatches_and_rejects_ui_only(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    async def fake_queue(request, server, graph):
        return "pid-tmpl"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    client = await _client(_FakeServer())
    runnable = {"output": {"9": {"class_type": "KSampler", "inputs": {"seed": 1}},
                           "15": {"class_type": "SaveImage", "inputs": {"filename_prefix": "t/run"}}}}
    await client.post("/v1/templates", json={"name": "Run Me", "graph": runnable}, headers={"X-Org": "acme"})
    r = await client.post("/v1/templates/render", json={"name": "run-me"}, headers={"X-Org": "acme"})
    assert r.status == 200 and (await r.json())["prompt_id"] == "pid-tmpl"
    assert sh.worklog.load("acme")[-1]["kind"] == "template"     # went through the ONE funnel
    # UI-only graph can't run headless -> 422 with a reason, not a silent dispatch
    await client.post("/v1/templates", json={"name": "UI Only", "graph": {"nodes": [], "last_node_id": 0}}, headers={"X-Org": "acme"})
    assert (await client.post("/v1/templates/render", json={"name": "ui-only"}, headers={"X-Org": "acme"})).status == 422
    # cross-tenant render: globex has no such template -> 404
    assert (await client.post("/v1/templates/render", json={"name": "run-me"}, headers={"X-Org": "globex"})).status == 404
    await client.close()


@pytest.mark.asyncio
async def test_workflow_reads_embedded_graph_tenant_scoped(tmp_path, monkeypatch):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    _orgaware(tmp_path, monkeypatch)
    root = Path(sh.folder_paths.get_org_output_directory("acme"))
    (root / "library.json").write_text('{"assets":[]}')
    (root / "fixes").mkdir(parents=True, exist_ok=True)
    info = PngInfo()
    ui = {"nodes": [{"id": 1, "type": "SaveImage"}], "last_node_id": 1}
    info.add_text("workflow", json.dumps(ui))
    info.add_text("prompt", json.dumps({"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fixes/x"}}}))
    Image.new("RGB", (4, 4)).save(root / "fixes" / "x_00001_.png", "PNG", pnginfo=info)
    sh.worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                      output_prefix="fixes/x", pid="p1")
    sh.worklog.record("acme", kind="fix", prompt="pending", refs=[], uploads=[], parents=[],
                      output_prefix="fixes/nope", pid="p2")
    client = await _client(_FakeServer())
    r = await client.get("/v1/workflow?id=p1", headers={"X-Org": "acme"})
    body = await r.json()
    assert r.status == 200 and body["format"] == "ui" and body["graph"] == ui   # UI graph preferred
    # not landed yet -> 404 (no graph to open)
    assert (await client.get("/v1/workflow?id=p2", headers={"X-Org": "acme"})).status == 404
    # cross-tenant id is never confirmed -> 404
    assert (await client.get("/v1/workflow?id=p1", headers={"X-Org": "globex"})).status == 404
    await client.close()


@pytest.mark.asyncio
async def test_wallet_proxies_and_scopes_org(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    async def fake_balance(tok):
        assert tok == "Bearer tkn"
        return {"availableCents": 2500, "currency": "usd", "pendingCents": 0}
    monkeypatch.setattr(sh, "_finance_balance", fake_balance)
    client = await _client(_FakeServer())
    r = await client.get("/v1/wallet", headers={"X-Org": "acme", "Authorization": "Bearer tkn"})
    body = await r.json()
    assert body == {"org": "acme", "balance": 25.0, "currency": "usd"}
    # signed-out (no bearer): display-only null balance, never a fabricated number
    r = await client.get("/v1/wallet", headers={"X-Org": "acme"})
    assert await r.json() == {"org": "acme", "balance": None, "currency": None}
    await client.close()


@pytest.mark.asyncio
async def test_library_upload_roundtrip_dedup_and_tenant_isolation(tmp_path, monkeypatch):
    import aiohttp
    _orgaware(tmp_path, monkeypatch)
    # library.json so _library_root resolves for the GET round-trip
    (Path(sh.folder_paths.get_org_output_directory("acme")) / "library.json").write_text('{"assets":[]}')
    (Path(sh.folder_paths.get_org_output_directory("globex")) / "library.json").write_text('{"assets":[]}')
    client = await _client(_FakeServer())
    png = b"\x89PNG\r\n\x1a\n" + b"acme-bytes"
    # multipart upload as acme -> renders/r1.png
    form = aiohttp.FormData()
    form.add_field("image", png, filename="r1.png", content_type="image/png")
    r = await client.post("/v1/library/upload", data=form, headers={"X-Org": "acme"})
    body = await r.json()
    assert r.status == 200 and body["ok"] and body["path"] == "renders/r1.png" and not body.get("existed")
    # round-trip through the org-scoped file server
    g = await client.get("/v1/library/file?path=renders/r1.png", headers={"X-Org": "acme"})
    assert g.status == 200 and (await g.read()) == png
    # dedup: identical name+size -> existed, no rewrite
    form2 = aiohttp.FormData()
    form2.add_field("image", png, filename="r1.png", content_type="image/png")
    r2 = await client.post("/v1/library/upload", data=form2, headers={"X-Org": "acme"})
    assert (await r2.json()) == {"ok": True, "existed": True, "path": "renders/r1.png"}
    # tenant isolation: org B never sees org A's file; its own upload is a separate tree
    assert (await client.get("/v1/library/file?path=renders/r1.png", headers={"X-Org": "globex"})).status == 404
    rg = await client.post("/v1/library/upload?name=r1.png", data=b"\x89PNG\r\n\x1a\nglobex",
                           headers={"X-Org": "globex", "Content-Type": "application/octet-stream"})
    assert (await rg.json())["path"] == "renders/r1.png"
    # org A's bytes are untouched by org B's same-named upload
    assert (await (await client.get("/v1/library/file?path=renders/r1.png", headers={"X-Org": "acme"})).read()) == png
    await client.close()


@pytest.mark.asyncio
async def test_library_upload_rejects_traversal_bad_name_and_oversize(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    hdr = {"X-Org": "acme", "Content-Type": "application/octet-stream"}
    # subpath traversal -> 400
    assert (await client.post("/v1/library/upload?name=x.png&subpath=../secret", data=b"\x89PNGx", headers=hdr)).status == 400
    # non-image name -> 400
    assert (await client.post("/v1/library/upload?name=notes.txt", data=b"hi", headers=hdr)).status == 400
    # a traversal-y NAME is neutralized to its basename, lands safely under renders/
    r = await client.post("/v1/library/upload?name=../../evil.png", data=b"\x89PNG\r\n\x1a\nevil", headers=hdr)
    assert (await r.json())["path"] == "renders/evil.png"
    assert (Path(sh.folder_paths.get_org_output_directory("acme")) / "renders" / "evil.png").is_file()
    # size cap -> 413
    monkeypatch.setattr(sh, "_MAX_UPLOAD", 8)
    assert (await client.post("/v1/library/upload?name=big.png", data=b"0123456789", headers=hdr)).status == 413
    await client.close()


@pytest.mark.asyncio
async def test_library_upload_indexes_into_assets_and_worklog(tmp_path, monkeypatch):
    import aiohttp
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    png = b"\x89PNG\r\n\x1a\n" + b"render-bytes"
    form = aiohttp.FormData()
    form.add_field("image", png, filename="shot.png", content_type="image/png")
    r = await client.post("/v1/library/upload?node=spark&design=karma&kind=render",
                          data=form, headers={"X-Org": "acme"})
    assert (await r.json())["path"] == "renders/shot.png"
    # instantly findable in Assets (library.json entry, draft)
    lib = await (await client.get("/v1/library", headers={"X-Org": "acme"})).json()
    a = next(x for x in lib["assets"] if x["path"] == "renders/shot.png")
    assert a["status"] == "draft" and a["kind"] == "render" and a["design"] == "karma" and a["tags"] == []
    # instantly findable in Queue & History with its source node + resolved output
    wl = await (await client.get("/v1/worklog", headers={"X-Org": "acme"})).json()
    it = next(x for x in wl["items"] if x["output_prefix"] == "renders/shot.png")
    assert it["kind"] == "render" and it["node"] == "spark" and it["status"] == "done"
    assert it["output"] == "renders/shot.png" and it["lane"] == "gpu"
    # tenant isolation: org B sees neither the asset nor the row
    lib2 = await (await client.get("/v1/library", headers={"X-Org": "globex"})).json()
    assert all(x["path"] != "renders/shot.png" for x in lib2.get("assets", []))
    assert (await (await client.get("/v1/worklog", headers={"X-Org": "globex"})).json())["items"] == []
    # dedup: re-upload identical bytes adds no second asset or row
    form2 = aiohttp.FormData()
    form2.add_field("image", png, filename="shot.png", content_type="image/png")
    r2 = await client.post("/v1/library/upload?node=spark", data=form2, headers={"X-Org": "acme"})
    assert (await r2.json()).get("existed") is True
    lib3 = await (await client.get("/v1/library", headers={"X-Org": "acme"})).json()
    assert sum(1 for x in lib3["assets"] if x["path"] == "renders/shot.png") == 1
    wl3 = await (await client.get("/v1/worklog", headers={"X-Org": "acme"})).json()
    assert sum(1 for x in wl3["items"] if x["output_prefix"] == "renders/shot.png") == 1
    await client.close()


@pytest.mark.asyncio
async def test_upload_schedules_quality_gate_only_for_new_renders(tmp_path, monkeypatch):
    """The gate fires at the ingest choke point exactly when a NEW asset row is
    created — a fresh render, or a restored-store file with no row yet — and never
    again for an identical re-post (dedup). Wiring only; the judgment runs
    out-of-band and is unit-tested against quality_gate.run."""
    import aiohttp
    _orgaware(tmp_path, monkeypatch)
    root = Path(sh.folder_paths.get_org_output_directory("acme"))
    (root / "library.json").write_text('{"assets":[]}')
    calls = []
    monkeypatch.setattr(sh.quality_gate, "schedule",
                        lambda request, org, r, rel: calls.append((org, rel)))
    client = await _client(_FakeServer())
    png = b"\x89PNG\r\n\x1a\n" + b"gate-bytes"

    form = aiohttp.FormData()
    form.add_field("image", png, filename="g.png", content_type="image/png")
    r = await client.post("/v1/library/upload", data=form, headers={"X-Org": "acme"})
    assert (await r.json())["path"] == "renders/g.png"
    assert calls == [("acme", "renders/g.png")]            # fresh render → judged once

    form2 = aiohttp.FormData()
    form2.add_field("image", png, filename="g.png", content_type="image/png")
    r2 = await client.post("/v1/library/upload", data=form2, headers={"X-Org": "acme"})
    assert (await r2.json()).get("existed") is True
    assert calls == [("acme", "renders/g.png")]            # identical re-post → NOT re-judged

    # a file already on disk but with no library row (restored store) → a new row → judged
    png2 = b"\x89PNG\r\n\x1a\n" + b"restored"
    (root / "renders" / "old.png").write_bytes(png2)
    form3 = aiohttp.FormData()
    form3.add_field("image", png2, filename="old.png", content_type="image/png")
    r3 = await client.post("/v1/library/upload", data=form3, headers={"X-Org": "acme"})
    assert (await r3.json()).get("existed") is True        # bytes already there (dedup write)
    assert calls == [("acme", "renders/g.png"), ("acme", "renders/old.png")]
    await client.close()


@pytest.mark.asyncio
async def test_status_route_accepts_flagged_and_restores(tmp_path, monkeypatch):
    """The one lifecycle route gains ``flagged`` (so the gate's status is a first-
    class one) and drives the UI's Restore back to draft. /v1/library still returns
    every status — the UI hides flagged; the backend never filters it away."""
    _orgaware(tmp_path, monkeypatch)
    root = Path(sh.folder_paths.get_org_output_directory("acme"))
    (root / "a.png").write_bytes(b"\x89PNG")
    (root / "library.json").write_text(json.dumps({"assets": [{"path": "a.png", "status": "draft"}]}))
    client = await _client(_FakeServer())
    r = await client.post("/v1/library/status", json={"path": "a.png", "status": "flagged"}, headers={"X-Org": "acme"})
    assert r.status == 200 and (await r.json())["status"] == "flagged"
    lib = await (await client.get("/v1/library", headers={"X-Org": "acme"})).json()
    assert next(x for x in lib["assets"] if x["path"] == "a.png")["status"] == "flagged"
    r2 = await client.post("/v1/library/status", json={"path": "a.png", "status": "draft"}, headers={"X-Org": "acme"})
    assert r2.status == 200 and (await r2.json())["status"] == "draft"
    assert (await client.post("/v1/library/status", json={"path": "a.png", "status": "bogus"}, headers={"X-Org": "acme"})).status == 400
    await client.close()


def test_studio_home_hides_flagged_and_offers_review():
    """UI contract: flagged is hidden from the normal grid exactly like deleted, and
    reachable for review + restore. /v1/library returns all statuses, so the grid is
    the filter — this locks the change I made to it."""
    html = sh._HTML.read_text()
    assert "a.status==='deleted'||a.status==='flagged'" in html   # hidden from normal views
    assert "FILTER='flagged'" in html                             # the Flagged review filter
    assert "showDel?'Recover':'Restore'" in html                  # restore affordance


@pytest.mark.asyncio
async def test_worklog_search_matches_paths_and_refs_not_just_prompt(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    (Path(sh.folder_paths.get_org_output_directory("acme")) / "library.json").write_text('{"assets":[]}')
    # an ingested render: empty prompt, findable only by its filename / ref
    sh.worklog.record("acme", kind="render", prompt="", refs=["srcphoto.png"], uploads=[],
                      parents=[], output_prefix="renders/model_shot.png", pid="r1", status="done")
    sh.worklog.record("acme", kind="fix", prompt="brighten the sky", refs=[], uploads=[],
                      parents=[], output_prefix="fixes/x", pid="r2")
    client = await _client(_FakeServer())

    async def ids(q):
        r = await client.get("/v1/worklog?q=" + q, headers={"X-Org": "acme"})
        return [it["id"] for it in (await r.json())["items"]]

    assert await ids("model_shot") == ["r1"]   # by output filename, empty prompt
    assert await ids("srcphoto") == ["r1"]      # by reference
    assert await ids("brighten") == ["r2"]      # prompt search still works
    assert await ids("nomatch") == []
    await client.close()


# ── Queue/History surface: favorites, stacks, ratings-with-notes, flow templates ──
@pytest.mark.asyncio
async def test_favorites_toggle_and_tenant_isolation(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    # empty to start
    assert (await (await client.get("/v1/favorites", headers={"X-Org": "acme"})).json()) == {"org": "acme", "favorites": {}}
    # favorite a run (key = run id) — echoes back, stamps a ts
    r = await client.post("/v1/favorites", json={"key": "run-1", "favorite": True}, headers={"X-Org": "acme"})
    assert r.status == 200 and (await r.json()) == {"ok": True, "key": "run-1", "favorite": True}
    favs = (await (await client.get("/v1/favorites", headers={"X-Org": "acme"})).json())["favorites"]
    assert "run-1" in favs and isinstance(favs["run-1"]["ts"], int)
    # favorite defaults to True when omitted
    await client.post("/v1/favorites", json={"key": "run-2"}, headers={"X-Org": "acme"})
    assert set((await (await client.get("/v1/favorites", headers={"X-Org": "acme"})).json())["favorites"]) == {"run-1", "run-2"}
    # un-favorite removes it (the heart toggles both ways)
    await client.post("/v1/favorites", json={"key": "run-1", "favorite": False}, headers={"X-Org": "acme"})
    assert list((await (await client.get("/v1/favorites", headers={"X-Org": "acme"})).json())["favorites"]) == ["run-2"]
    # validation: empty key -> 400
    assert (await client.post("/v1/favorites", json={"key": ""}, headers={"X-Org": "acme"})).status == 400
    # tenant isolation: globex sees none of acme's favorites
    assert (await (await client.get("/v1/favorites", headers={"X-Org": "globex"})).json())["favorites"] == {}
    await client.close()


@pytest.mark.asyncio
async def test_stacks_save_normalizes_and_tenant_isolation(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    assert (await (await client.get("/v1/stacks", headers={"X-Org": "acme"})).json()) == {"org": "acme", "stacks": []}
    payload = {"stacks": [
        {"id": "s1", "name": "Karma", "items": ["run-a", "run-b", "run-b", "run-a"], "ts": 100},  # items de-duped
        {"id": "s2", "items": ["only-one"]},      # < 2 members -> dropped (not a folder)
        {"id": "", "items": ["x", "y"]},          # no id -> dropped
        {"name": "bad"},                          # no id -> dropped
    ]}
    stacks = (await (await client.post("/v1/stacks", json=payload, headers={"X-Org": "acme"})).json())["stacks"]
    assert stacks == [{"id": "s1", "name": "Karma", "items": ["run-a", "run-b"], "ts": 100}]
    # persisted for the org (whole-set replace is the ONE write)
    assert (await (await client.get("/v1/stacks", headers={"X-Org": "acme"})).json())["stacks"] == stacks
    # name defaults when omitted
    r2 = await client.post("/v1/stacks", json={"stacks": [{"id": "s3", "items": ["run-c", "run-d"]}]}, headers={"X-Org": "acme"})
    assert (await r2.json())["stacks"][0]["name"] == "Stack"
    # tenant isolation: globex has its own (empty) set, untouched by acme's writes
    assert (await (await client.get("/v1/stacks", headers={"X-Org": "globex"})).json())["stacks"] == []
    await client.close()


@pytest.mark.asyncio
async def test_rate_stars_notes_path_and_tenant_isolation(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    # rate resolves via _library_root -> the org needs a library.json
    (Path(sh.folder_paths.get_org_output_directory("acme")) / "library.json").write_text('{"assets":[]}')
    (Path(sh.folder_paths.get_org_output_directory("globex")) / "library.json").write_text('{"assets":[]}')
    client = await _client(_FakeServer())
    # 0-5 stars + notes + the run's output path, keyed by run id (the flywheel row)
    r = await client.post("/v1/library/rate", json={
        "key": "run-1", "stars": 4, "notes": "hands look off", "path": "fixes/x_00001_.png"},
        headers={"X-Org": "acme"})
    assert r.status == 200
    entry = (await r.json())["entry"]
    assert entry["stars"] == 4 and entry["notes"] == "hands look off" and entry["path"] == "fixes/x_00001_.png"
    # visible on reopen; a note-only update keeps the stars (partial update)
    await client.post("/v1/library/rate", json={"key": "run-1", "notes": "better now"}, headers={"X-Org": "acme"})
    ratings = await (await client.get("/v1/library/ratings", headers={"X-Org": "acme"})).json()
    assert ratings["run-1"]["stars"] == 4 and ratings["run-1"]["notes"] == "better now"
    # a 0-star rating is valid (0-5 inclusive)
    assert (await client.post("/v1/library/rate", json={"key": "run-1", "stars": 0}, headers={"X-Org": "acme"})).status == 200
    # validation: out of range -> 400; missing key -> 400
    assert (await client.post("/v1/library/rate", json={"key": "r", "stars": 6}, headers={"X-Org": "acme"})).status == 400
    assert (await client.post("/v1/library/rate", json={"key": "", "stars": 3}, headers={"X-Org": "acme"})).status == 400
    # tenant isolation: globex never sees acme's rating
    assert (await (await client.get("/v1/library/ratings", headers={"X-Org": "globex"})).json()) == {}
    await client.close()


@pytest.mark.asyncio
async def test_template_accepts_flow_payload_not_just_graph(tmp_path, monkeypatch):
    _orgaware(tmp_path, monkeypatch)
    client = await _client(_FakeServer())
    # a flow template (no node graph) — what "favorite -> turn into a template" saves
    flow = {"kind": "fix", "prompt": "make the straps thicker", "refs": ["a.png"], "parents": ["a.png"]}
    r = await client.post("/v1/templates", json={"name": "Thicken Straps", "flow": flow}, headers={"X-Org": "acme"})
    assert r.status == 200 and (await r.json())["slug"] == "thicken-straps"
    # lists (not renderable — no runnable graph) and round-trips the captured flow
    lst = (await (await client.get("/v1/templates", headers={"X-Org": "acme"})).json())["templates"]
    assert lst[0]["name"] == "Thicken Straps" and lst[0]["renderable"] is False
    got = await (await client.get("/v1/templates/thicken-straps", headers={"X-Org": "acme"})).json()
    assert got["flow"] == flow and "graph" not in got
    # neither graph nor flow -> 400 (a template must capture something)
    assert (await client.post("/v1/templates", json={"name": "empty"}, headers={"X-Org": "acme"})).status == 400
    await client.close()
