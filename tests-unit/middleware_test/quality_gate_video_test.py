"""Quality gate — the VIDEO judge (middleware/quality_gate.py).

The motion gate samples frames across a clip and asks the MOTION judge; a clear FAIL
flags the row exactly like the image gate. A video whose frames can't be sampled is
marked visibly UNGATED (gate='skipped' + an 'ungated' work-log event) — NEVER a silent
pass. The image path still fails open, byte-for-byte unchanged.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import quality_gate as qg
from middleware import worklog as wl

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


def _write_mp4(path: Path, frames: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = av.open(str(path), mode="w")
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


@pytest.fixture
def orgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory", lambda o=None: str(tmp_path))
    return tmp_path


# ── frame sampling ────────────────────────────────────────────────────────────────
def test_frames_samples_video(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    _write_mp4(mp4)
    frames = qg._frames(mp4)
    assert frames is not None and 1 <= len(frames) <= 3
    assert all(isinstance(b, str) and b for b in frames)      # base64 JPEG strings

    # a garbage container yields None (→ ungated, never a silent pass)
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00 ftyp" + b"garbage")
    assert qg._frames(bad) is None


# ── the payload carries N frames + the MOTION prompt ──────────────────────────────
@pytest.mark.asyncio
async def test_video_judge_payload(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "PASS"}}]}

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen["payload"] = json
            return _Resp()

    monkeypatch.setattr(qg.aiohttp, "ClientSession", _Session)
    reply = await qg.judge(["b64one", "b64two"], "Bearer u", qg.MOTION)
    assert reply == "PASS"
    content = seen["payload"]["messages"][0]["content"]
    texts = [c for c in content if c["type"] == "text"]
    images = [c for c in content if c["type"] == "image_url"]
    assert len(texts) == 1 and texts[0]["text"] == qg.MOTION
    assert len(images) == 2                                    # one part per frame


# ── a clear FAIL flags the row ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_video_fail_flags(orgdir, monkeypatch):
    root = orgdir
    _write_mp4(root / "video" / "c.mp4")
    (root / "library.json").write_text(json.dumps(
        {"assets": [{"path": "video/c.mp4", "status": "draft"}]}))

    async def fake_judge(images, bearer, prompt):
        assert prompt == qg.MOTION                             # the motion contract, not PROMPT
        return "FAIL — hair morphs between frames"

    monkeypatch.setattr(qg, "judge", fake_judge)
    await qg.run("acme", root, "video/c.mp4", "")
    lib = json.loads((root / "library.json").read_text())
    assert lib["assets"][0]["status"] == "flagged"


# ── an unsamplable video is UNGATED — badge + event, never silent ─────────────────
@pytest.mark.asyncio
async def test_unreadable_video_is_ungated_not_silent(orgdir, monkeypatch):
    root = orgdir
    (root / "video").mkdir(parents=True, exist_ok=True)
    (root / "video" / "broken.mp4").write_bytes(b"\x00\x00\x00 ftyp" + b"not decodable")
    (root / "library.json").write_text(json.dumps(
        {"assets": [{"path": "video/broken.mp4", "status": "draft"}]}))
    # a work-log row for this output so the 'ungated' event has somewhere to land
    wl.record("acme", kind="video", prompt="", refs=[], uploads=[], parents=[],
              output_prefix="video/broken.mp4", pid="up-x", status="done")

    async def never(*a, **k):
        raise AssertionError("judge must not be called for an unsamplable video")

    monkeypatch.setattr(qg, "judge", never)
    await qg.run("acme", root, "video/broken.mp4", "")

    lib = json.loads((root / "library.json").read_text())
    assert lib["assets"][0].get("gate") == "skipped"          # the visible badge field
    assert lib["assets"][0]["status"] == "draft"              # status untouched
    events = [e["s"] for r in wl.load("acme") for e in r.get("events", [])]
    assert "ungated" in events                                # the work-log trail, not silent


# ── the image path still fails OPEN, unchanged ────────────────────────────────────
@pytest.mark.asyncio
async def test_image_still_fails_open(orgdir, monkeypatch):
    root = orgdir
    (root / "x.png").write_bytes(b"not a real png")            # unreadable → _frames None
    (root / "library.json").write_text(json.dumps(
        {"assets": [{"path": "x.png", "status": "draft"}]}))

    async def never(*a, **k):
        raise AssertionError("judge must not run on an unreadable image")

    monkeypatch.setattr(qg, "judge", never)
    await qg.run("acme", root, "x.png", "")
    lib = json.loads((root / "library.json").read_text())
    assert lib["assets"][0]["status"] == "draft"              # fail open: not flagged
    assert "gate" not in lib["assets"][0]                     # and NOT marked skipped (images fail open)
