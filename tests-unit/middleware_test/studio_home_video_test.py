"""Unit tests for the Studio video lane (middleware/studio_home.py):

  * the Wan 2.2 TI2V-5B graph builder (t2v omits the reference node; clamps + snaps);
  * dispatch + the /v1/library/video route (org from the token, never the body);
  * landed-output + queue detection across .mp4/.webm (the honesty guard covers video);
  * the ONE indexed ingest door accepts video, defaults kind, judges, colour-matches;
  * the manifest sweeper keeps a video as kind="video";
  * serving + the first-frame poster (404 on decode failure, never the raw mp4);
  * embedded-metadata read parity (Versions / Re-run / provenance) and job re-tagging.

Filesystem + av only — no torch, no GPU, no network. av/numpy are hard studio deps.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from middleware import studio_home as sh
from middleware import worklog as wl

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


# ── media fixtures: real, tiny, in-test-encoded ───────────────────────────────────
def _write_mp4(path: Path, frames: int = 6, meta: dict | None = None) -> None:
    """A tiny real mp4 (ftyp at offset 4). ``meta`` embeds container metadata the way
    SaveVideo does — via movflags=use_metadata_tags, so av reads it back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    opts = {"movflags": "use_metadata_tags"} if meta else {}
    c = av.open(str(path), mode="w", options=opts)
    for k, v in (meta or {}).items():
        c.metadata[k] = v
    st = c.add_stream("libx264", rate=24)
    st.width, st.height, st.pix_fmt = 64, 64, "yuv420p"
    for i in range(frames):
        arr = np.full((64, 64, 3), (i * 40) % 255, dtype=np.uint8)
        fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in st.encode(fr):
            c.mux(pkt)
    for pkt in st.encode():
        c.mux(pkt)
    c.close()


def _write_webm(path: Path, frames: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = av.open(str(path), mode="w")
    st = c.add_stream("libvpx-vp9", rate=24)
    st.width, st.height, st.pix_fmt = 64, 64, "yuv420p"
    for i in range(frames):
        arr = np.full((64, 64, 3), (i * 40) % 255, dtype=np.uint8)
        fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in st.encode(fr):
            c.mux(pkt)
    for pkt in st.encode():
        c.mux(pkt)
    c.close()


# ── request + server doubles ──────────────────────────────────────────────────────
class _Req:
    """A minimal stand-in for an aiohttp request the route handlers read: JSON body,
    query, headers, cookies, and the iam_user the middleware would have attached."""
    def __init__(self, *, body=None, raw=b"", query=None, headers=None,
                 org="acme", cookies=None, content_length=None):
        self._body = body
        self._raw = raw
        self.query = query or {}
        self.headers = headers or {}
        self._user = {"org_id": org, "sub": f"u@{org}"} if org else None
        self.cookies = cookies or {}
        self.content_length = content_length if content_length is not None else (len(raw) or None)

    def get(self, key, default=None):
        return self._user if key == "iam_user" else default

    async def json(self):
        return self._body if self._body is not None else {}

    async def read(self):
        return self._raw


class _FakeQueue:
    def get_current_queue_volatile(self):
        return ([], [])


class _FakeServer:
    port = 1

    def __init__(self):
        self.prompt_queue = _FakeQueue()


@pytest.fixture
def orgaware(tmp_path, monkeypatch):
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
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory", outd)
    return outd, ind


def _handler(server, method, path, monkeypatch):
    """Extract ONE registered route handler, without the stacks sub-router or a live app."""
    monkeypatch.setattr(sh.stacks_store, "add_stacks_routes", lambda *a, **k: None)
    routes = web.RouteTableDef()
    sh.add_studio_home_routes(routes, server)
    for r in routes:
        if getattr(r, "method", None) == method and getattr(r, "path", None) == path:
            return r.handler
    raise AssertionError(f"no route {method} {path}")


def _lib(root: Path, assets):
    (root / "library.json").write_text(json.dumps({"assets": assets}))


# ── 1. the graph builder ──────────────────────────────────────────────────────────
def test_video_graph():
    # text-to-video: NO LoadImage node, and NO start_image key on the latent
    t2v = sh._video_graph("a woman walks on a sunlit beach", "video/x", length=73)
    assert "7" not in t2v
    assert "start_image" not in t2v["8"]["inputs"]
    assert t2v["8"]["inputs"]["length"] == 73                 # 4n+1, unchanged

    # image-to-video: LoadImage → start_image; clamp/snap of the dims + length
    g = sh._video_graph("beach scene", "video/y_ab12cd", ref_name="ref.png",
                        width=650, height=641, length=72)
    assert g["7"]["class_type"] == "LoadImage" and g["7"]["inputs"]["image"] == "ref.png"
    assert g["8"]["inputs"]["start_image"] == ["7", 0]
    assert (g["8"]["inputs"]["width"], g["8"]["inputs"]["height"]) == (640, 640)   # step of 32
    assert g["8"]["inputs"]["length"] == 69                   # 72 → nearest 4n+1 below

    ks = g["9"]["inputs"]
    assert ks["steps"] == 20 and ks["cfg"] == 5.0
    assert ks["sampler_name"] == "uni_pc" and ks["scheduler"] == "simple"
    assert g["4"]["class_type"] == "ModelSamplingSD3" and g["4"]["inputs"]["shift"] == 8
    sv = g["12"]
    assert sv["class_type"] == "SaveVideo"
    assert sv["inputs"] == {"video": ["11", 0], "filename_prefix": "video/y_ab12cd",
                            "format": "mp4", "codec": "h264"}
    assert g["6"]["inputs"]["text"] == sh._WAN_NEG
    assert sh._WAN_NEG.startswith("色调艳丽") and "倒着走" in sh._WAN_NEG

    # lessons steer the NEGATIVE encoder only, exactly like _fix_graph — the
    # positive never carries the words being avoided
    gl = sh._video_graph("beach", "video/z", lessons=["hair morphs between frames"])
    assert "hair morphs between frames" not in gl["5"]["inputs"]["text"]
    assert "hair morphs between frames" in gl["6"]["inputs"]["text"]


# ── 2. dispatch + the route (org from the token, not the body) ─────────────────────
@pytest.mark.asyncio
async def test_dispatch_route(orgaware, monkeypatch):
    outd, _ = orgaware
    captured = {}

    async def fake_queue(request, server, graph):
        captured["graph"] = graph
        return "pid-v", ""

    monkeypatch.setattr(sh, "_submit", fake_queue)
    video = _handler(_FakeServer(), "POST", "/v1/library/video", monkeypatch)

    # an 'org' field in the body is IGNORED — the row lands under the TOKEN org
    resp = await video(_Req(body={"prompt": "a woman walking on a beach at sunset",
                                  "org": "evil"}, org="acme"))
    assert resp.status == 200
    out = json.loads(resp.text)
    import re
    assert re.match(r"^video/.+_[0-9a-f]{6}$", out["output_prefix"])
    rows = wl.load("acme")
    assert len(rows) == 1 and rows[0]["kind"] == "video"
    assert rows[0]["lane"] in ("local", "gpu")
    assert wl.load("evil") == []                              # never under the body's org
    # the graph that queued is the Wan graph (t2v: no LoadImage)
    assert any(n.get("class_type") == "SaveVideo" for n in captured["graph"].values())
    assert not any(n.get("class_type") == "LoadImage" for n in captured["graph"].values())

    # prompt < 3 chars → 400
    r2 = await video(_Req(body={"prompt": "hi"}, org="acme"))
    assert r2.status == 400

    # two references (a library path AND an upload) → 400
    r3 = await video(_Req(body={"prompt": "a valid video prompt", "path": "a.png",
                                "upload": "u.png"}, org="acme"))
    assert r3.status == 400


# ── 3. landed-output + the honesty guard, over video ──────────────────────────────
def test_landed_video(orgaware):
    outd, _ = orgaware
    root = Path(outd("acme"))
    (root / "video").mkdir(parents=True, exist_ok=True)
    (root / "video" / "x_ab12cd_00001_.mp4").write_bytes(b"\x00\x00\x00 ftyp")
    assert sh._landed_output(root, "video/x_ab12cd") == "video/x_ab12cd_00001_.mp4"
    (root / "video" / "w_00001_.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    assert sh._landed_output(root, "video/w") == "video/w_00001_.webm"

    # child-prefix must NOT be swallowed: "a" never claims "a_00001__00001_.mp4"
    (root / "video" / "a_00001__00001_.mp4").write_bytes(b"\x00\x00\x00 ftyp")
    assert sh._landed_output(root, "video/a") is None

    # png unchanged
    (root / "fixes").mkdir(parents=True, exist_ok=True)
    (root / "fixes" / "y_00001_.png").write_bytes(b"\x89PNG\r\n")
    assert sh._landed_output(root, "fixes/y") == "fixes/y_00001_.png"

    # _finalize_landed marks the video row done at the file mtime …
    wl.record("acme", kind="video", prompt="p", refs=[], uploads=[], parents=[],
              output_prefix="video/done", params={"steps": 20}, pid="dv", status="queued")
    ts = next(r["ts"] for r in wl.load("acme") if r["id"] == "dv")
    dp = root / "video" / "done_00001_.mp4"
    dp.write_bytes(b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x08free")
    os.utime(dp, (ts + 200, ts + 200))
    rows = wl.load("acme")
    sh._finalize_landed("acme", root, rows, int(time.time()) + 500)
    assert next(r for r in wl.load("acme") if r["id"] == "dv")["status"] == "done"

    # … but a <5s landing for a 20-step render is impossible → failed
    wl.record("acme", kind="video", prompt="p", refs=[], uploads=[], parents=[],
              output_prefix="video/fast", params={"steps": 20}, pid="fv", status="queued")
    tsf = next(r["ts"] for r in wl.load("acme") if r["id"] == "fv")
    fp = root / "video" / "fast_00001_.mp4"
    fp.write_bytes(b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x08free")
    os.utime(fp, (tsf + 2, tsf + 2))
    rows = wl.load("acme")
    sh._finalize_landed("acme", root, rows, int(time.time()) + 500)
    failed = next(r for r in wl.load("acme") if r["id"] == "fv")
    assert failed["status"] == "failed"
    assert any("did not sample" in (e.get("note") or "") for e in failed.get("events", []))


# ── 4. the live BYO-GPU queue drops a video job when its mp4 lands ─────────────────
@pytest.mark.asyncio
async def test_render_queue_video(orgaware, monkeypatch):
    """The render-queue is AUTHORITATIVE: it reads the org's gpu-jobs activities from the
    tasks engine and shows the REAL claiming GPU (activity.identity), dropping terminal
    jobs. A STARTED studio.render is live; a COMPLETED one falls off."""
    outd, _ = orgaware
    root = Path(outd("acme"))
    _lib(root, [])
    render_queue = _handler(_FakeServer(), "GET", "/v1/render-queue", monkeypatch)
    base = {
        "execution": {"runId": "p", "workflowId": "p"},
        "type": {"name": "studio.render"},
        "taskQueue": "gpu:spark", "identity": "spark",
        "input": {"prompt": {"12": {"class_type": "SaveVideo",
                                    "inputs": {"filename_prefix": "video/x"}}}},
    }

    async def _started(_req):
        return [{**base, "status": "STARTED"}]
    monkeypatch.setattr(sh, "_list_gpu_jobs", _started)
    resp = await render_queue(_Req(org="acme"))
    jobs = json.loads(resp.text)["jobs"]
    assert len(jobs) == 1 and jobs[0]["node"] == "spark"       # real claimer shown

    async def _completed(_req):
        return [{**base, "status": "COMPLETED"}]
    monkeypatch.setattr(sh, "_list_gpu_jobs", _completed)
    resp = await render_queue(_Req(org="acme"))
    assert json.loads(resp.text)["jobs"] == []                 # engine says done → dropped


# ── 5. worklog params + per-kind ETA bucketing ────────────────────────────────────
def test_graph_params_video(orgaware):
    outd, _ = orgaware
    g = sh._video_graph("a beach", "video/x", seed=7, length=73)
    p = sh._graph_params(g)
    for k in ("steps", "cfg", "sampler", "scheduler", "seed", "width", "height", "length", "fps"):
        assert k in p, k
    assert p["fps"] == 24 and p["length"] == 73 and p["seed"] == 7

    wl.record("acme", kind="video", prompt="", refs=[], uploads=[], parents=[],
              output_prefix="video/a", pid="v1", status="queued")
    wl.mark_started("acme", "v1", 1000)
    wl.mark_finished("acme", "v1", 1120)
    wl.record("acme", kind="fix", prompt="", refs=[], uploads=[], parents=[],
              output_prefix="fixes/a", pid="f1", status="queued")
    wl.mark_started("acme", "f1", 1000)
    wl.mark_finished("acme", "f1", 1030)
    medians = wl.median_durations(wl.load("acme"))
    assert medians.get("video") == 120 and medians.get("fix") == 30   # bucketed apart


# ── 6. the ONE indexed ingest door accepts video ──────────────────────────────────
@pytest.mark.asyncio
async def test_upload_video(orgaware, tmp_path, monkeypatch):
    outd, _ = orgaware
    root = Path(outd("acme"))
    _lib(root, [])
    scheduled = {}
    cm = {}
    monkeypatch.setattr(sh.quality_gate, "schedule",
                        lambda request, org, r, rel: scheduled.update(rel=rel, org=org))
    monkeypatch.setattr(sh, "colormatch_fix_ingest",
                        lambda request, sub, name: cm.update(sub=sub, name=name))
    monkeypatch.setattr(sh.stacks_store, "absorb", lambda *a, **k: None)   # no stacks DB in this unit
    upload = _handler(_FakeServer(), "POST", "/v1/library/upload", monkeypatch)

    src = tmp_path / "clip.mp4"
    _write_mp4(src)
    mp4 = src.read_bytes()

    resp = await upload(_Req(raw=mp4, query={"name": "clip.mp4"}, org="acme"))
    body = json.loads(resp.text)
    assert resp.status == 200 and body["path"] == "renders/clip.mp4"
    assert (root / "renders" / "clip.mp4").is_file()
    # kind defaults to "video" by suffix
    lib = json.loads((root / "library.json").read_text())
    row = next(a for a in lib["assets"] if a["path"] == "renders/clip.mp4")
    assert row["kind"] == "video"
    # the judge fired on the created row; the colour-restore hook saw (sub, name)
    assert scheduled.get("rel") == "renders/clip.mp4"
    assert cm == {"sub": "renders", "name": "clip.mp4"}

    # webm (EBML) accepted too
    wsrc = tmp_path / "clip.webm"
    _write_webm(wsrc)
    r_web = await upload(_Req(raw=wsrc.read_bytes(), query={"name": "clip.webm"}, org="acme"))
    assert r_web.status == 200

    # garbage named .mp4 → rejected; ftyp at OFFSET 0 (not 4) → rejected (regression pin)
    r_bad = await upload(_Req(raw=b"totally not a video, just text bytes here", query={"name": "x.mp4"}, org="acme"))
    assert r_bad.status == 400
    r_off0 = await upload(_Req(raw=b"ftyp" + b"\x00" * 40, query={"name": "y.mp4"}, org="acme"))
    assert r_off0.status == 400

    # over the 256MB ceiling → 413 (checked via content_length, no giant allocation)
    r_big = await upload(_Req(raw=mp4, query={"name": "big.mp4"}, org="acme",
                              content_length=sh._MAX_UPLOAD + 1))
    assert r_big.status == 413


# ── 7. the manifest sweeper keeps a video as kind="video" ─────────────────────────
def test_manifest_video(tmp_path):
    from scripts import library_manifest as lm
    root = tmp_path
    (root / "video").mkdir()
    (root / "video" / "x.mp4").write_bytes(b"\x00\x00\x00 ftyp")
    (root / "video" / "empty.mp4").write_bytes(b"")            # zero-byte → skipped
    assert "video/x.mp4" in lm.scan(str(root))
    assert "video/empty.mp4" not in lm.scan(str(root))

    doc = lm.build(str(root))
    row = next(a for a in doc["assets"] if a["path"] == "video/x.mp4")
    assert row["kind"] == "video" and row["status"] == "draft"

    # a curated status + tags survive the next sweep (the erase-every-cycle bug stays dead)
    lm.write_atomic(str(root), {"assets": [
        {"path": "video/x.mp4", "kind": "video", "status": "approved", "tags": ["hero"]}]})
    doc2 = lm.build(str(root))
    row2 = next(a for a in doc2["assets"] if a["path"] == "video/x.mp4")
    assert row2["kind"] == "video" and row2["status"] == "approved" and row2["tags"] == ["hero"]


# ── 8. serving + the first-frame poster ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_serve_and_poster(orgaware, monkeypatch):
    outd, _ = orgaware
    root = Path(outd("acme"))
    _write_mp4(root / "video" / "clip.mp4")
    _lib(root, [{"path": "video/clip.mp4", "kind": "video", "status": "draft"}])
    get_file = _handler(_FakeServer(), "GET", "/v1/library/file", monkeypatch)

    # full file served as video/mp4
    resp = await get_file(_Req(query={"path": "video/clip.mp4"}, org="acme"))
    assert resp.content_type == "video/mp4"

    # ?thumb=1 → a cached JPEG poster in .thumbs/<key>.jpg
    tresp = await get_file(_Req(query={"path": "video/clip.mp4", "thumb": "1"}, org="acme"))
    assert isinstance(tresp, web.FileResponse)
    key = __import__("re").sub(r"[^a-zA-Z0-9]", "_", str((root / "video" / "clip.mp4").resolve()))[-120:]
    assert (root / ".thumbs" / f"{key}.jpg").is_file()

    # a poster that cannot decode → 404, NEVER the raw mp4 bytes (use an un-cached path)
    _write_mp4(root / "video" / "clip2.mp4")
    _lib(root, [{"path": "video/clip.mp4"}, {"path": "video/clip2.mp4"}])

    def boom(*a, **k):
        raise RuntimeError("cannot decode")

    monkeypatch.setattr(av, "open", boom)
    r404 = await get_file(_Req(query={"path": "video/clip2.mp4", "thumb": "1"}, org="acme"))
    assert isinstance(r404, web.Response) and r404.status == 404
    assert not isinstance(r404, web.FileResponse)


# ── 9. embedded-metadata read parity: provenance, Open-in-Editor, Re-run ──────────
@pytest.mark.asyncio
async def test_embedded_and_rerun(orgaware, monkeypatch):
    outd, _ = orgaware
    root = Path(outd("acme"))
    graph = sh._video_graph("a woman on a beach", "video/x_ab12cd", seed=42, length=73)
    mp4 = root / "video" / "x_ab12cd_00001_.mp4"
    _write_mp4(mp4, meta={"prompt": json.dumps(graph)})
    _lib(root, [{"path": "video/x_ab12cd_00001_.mp4", "kind": "video", "status": "draft"}])

    # _embedded reads the container metadata back
    info = sh._embedded(mp4)
    assert "prompt" in info and json.loads(info["prompt"])["12"]["class_type"] == "SaveVideo"

    # _provenance yields the video params
    prov = sh._provenance(mp4)
    assert prov["workflow"] == "video/x_ab12cd" and prov["steps"] == 20
    assert prov["cfg"] == 5.0 and prov["seed"] == 42 and prov["length"] == 73 and prov["fps"] == 24

    # GET /v1/workflow serves format 'api' for the done video row. The dispatch ts must
    # predate the file mtime (else finalize reads the mp4 as a pre-existing sibling).
    ts = int(mp4.stat().st_mtime) - 100
    wl.record("acme", kind="video", prompt="p", refs=[], uploads=[], parents=[],
              output_prefix="video/x_ab12cd", params={"steps": 20}, pid="pv", status="queued")
    rows = wl.load("acme")
    for x in rows:
        if x["id"] == "pv":
            x["ts"] = ts
    wl._save("acme", rows)
    get_wf = _handler(_FakeServer(), "GET", "/v1/workflow", monkeypatch)
    wfresp = await get_wf(_Req(query={"id": "pv"}, org="acme"))
    wf = json.loads(wfresp.text)
    assert wf["format"] == "api" and wf["graph"]["12"]["class_type"] == "SaveVideo"

    # POST /v1/library/rerun dispatches kind='rerun' with a FRESH SaveVideo tag
    captured = {}

    async def fake_queue(request, server, g):
        captured["graph"] = g
        return "pid-rerun", ""

    monkeypatch.setattr(sh, "_submit", fake_queue)
    rerun = _handler(_FakeServer(), "POST", "/v1/library/rerun", monkeypatch)
    rr = await rerun(_Req(body={"path": "video/x_ab12cd_00001_.mp4"}, org="acme"))
    assert rr.status == 200
    sv = next(n for n in captured["graph"].values() if n["class_type"] == "SaveVideo")
    import re
    assert re.match(r"^video/x_[0-9a-f]{6}$", sv["inputs"]["filename_prefix"])
    assert sv["inputs"]["filename_prefix"] != "video/x_ab12cd"     # a fresh namespace
    assert any(r2["kind"] == "rerun" for r2 in wl.load("acme"))


# ── 10. template re-tag: video AND image savers get a fresh job identity ──────────
@pytest.mark.asyncio
async def test_template_retag(orgaware, monkeypatch):
    import re
    outd, _ = orgaware
    tdir = Path(outd("acme")).parent / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    captured = {}

    async def fake_queue(request, server, g):
        captured["graph"] = g
        return "pid-t", ""

    monkeypatch.setattr(sh, "_submit", fake_queue)
    render = _handler(_FakeServer(), "POST", "/v1/templates/render", monkeypatch)

    # a template whose graph saves via SaveVideo
    vgraph = sh._video_graph("a beach", "video/tmpl", length=73)
    (tdir / "clip.json").write_text(json.dumps({"name": "Clip", "slug": "clip", "graph": vgraph}))
    resp = await render(_Req(body={"name": "clip"}, org="acme"))
    assert resp.status == 200
    sv = next(n for n in captured["graph"].values() if n["class_type"] == "SaveVideo")
    assert re.match(r"^video/tmpl_[0-9a-f]{6}$", sv["inputs"]["filename_prefix"])

    # a SaveImage template is re-tagged too (the unique-job-tag rule holds for images)
    igraph = {"9": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 30}},
              "15": {"class_type": "SaveImage", "inputs": {"filename_prefix": "templates/pic"}}}
    (tdir / "pic.json").write_text(json.dumps({"name": "Pic", "slug": "pic", "graph": igraph}))
    resp2 = await render(_Req(body={"name": "pic"}, org="acme"))
    assert resp2.status == 200
    si = next(n for n in captured["graph"].values() if n["class_type"] == "SaveImage")
    assert re.match(r"^templates/pic_[0-9a-f]{6}$", si["inputs"]["filename_prefix"])
