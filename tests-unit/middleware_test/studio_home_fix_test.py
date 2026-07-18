"""Unit tests for the fix pipeline (middleware/studio_home.py):

  * the fix/compose graphs name only models that exist on the worker AND carry none
    of the Flux-Kontext reference-method wiring that stippled Qwen edits;
  * the honesty guard flips an implausibly-fast completion to FAILED;
  * iteration hygiene falls the fix base back to the nearest clean ancestor.

Pure/filesystem only — no torch, no GPU, no network.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import studio_home as sh

# What the render worker actually has on disk (spark: models/{diffusion_models,
# text_encoders,vae}). The graph must never name anything outside this.
AVAILABLE = {
    "diffusion_models": {"qwen-image-edit-2511.safetensors", "flux1-fill-dev.safetensors",
                         "flux-2-klein-4b.safetensors", "wan2.2_ti2v_5B_fp16.safetensors"},
    "text_encoders": {"qwen2.5-vl-7b.safetensors", "clip_l.safetensors",
                      "t5xxl_fp16.safetensors", "flux2-text-encoder.safetensors"},
    "vae": {"qwen-image-vae.safetensors", "flux-ae.safetensors", "flux2-vae.safetensors"},
}
_LOADERS = {"UNETLoader": ("diffusion_models", "unet_name"),
            "CheckpointLoaderSimple": ("checkpoints", "ckpt_name"),
            "CLIPLoader": ("text_encoders", "clip_name"),
            "VAELoader": ("vae", "vae_name")}


def _assert_models_exist(graph):
    checked = 0
    for node in graph.values():
        spec = _LOADERS.get(node.get("class_type"))
        if not spec:
            continue
        folder, key = spec
        name = node["inputs"][key]
        assert name in AVAILABLE[folder], f"{name!r} is not on the worker ({folder})"
        checked += 1
    return checked


# ── graph construction ───────────────────────────────────────────────────────────

def test_fix_graph_names_only_existing_models():
    g = sh._fix_graph("ref.png", "brighten the skin tone", "fixes/x")
    assert _assert_models_exist(g) == 3            # unet + clip + vae all verified


def test_fix_graph_is_the_canonical_qwen_edit():
    """ComfyUI's own Qwen-Image-Edit template: NO Flux-Kontext reference-method
    override (the stipple cause), and the SAME FluxKontextImageScale'd reference feeds
    BOTH the edit conditioning and the VAEEncode sampling latent — matched size, so no
    frame-within-frame border."""
    g = sh._fix_graph("ref.png", "recolor the top", "fixes/x")
    types = [n["class_type"] for n in g.values()]
    assert "FluxKontextMultiReferenceLatentMethod" not in types   # the stipple cause
    assert types.count("TextEncodeQwenImageEditPlus") == 2        # positive + negative
    ks = next(n for n in g.values() if n["class_type"] == "KSampler")
    assert ks["inputs"]["denoise"] == 1.0
    latent = g[ks["inputs"]["latent_image"][0]]
    assert latent["class_type"] == "VAEEncode"
    scaled = latent["inputs"]["pixels"]                           # the sampling latent's source
    assert g[scaled[0]]["class_type"] == "FluxKontextImageScale"  # the scaled reference
    assert g["6"]["inputs"]["image1"] == scaled                   # conditioning uses the SAME scaled ref


def test_compose_graph_clean_family_and_models():
    g = sh._compose_graph(["a.png", "b.png"], "merge the two looks", "composes/x")
    types = [n["class_type"] for n in g.values()]
    assert "FluxKontextMultiReferenceLatentMethod" not in types
    assert _assert_models_exist(g) == 3


# ── the honesty guard ─────────────────────────────────────────────────────────────

def test_implausibly_fast_only_flags_unsampled_renders():
    now = 1_000_000
    r30 = {"ts": now, "params": {"steps": 30}}
    assert sh._implausibly_fast(r30, now + 1) is True        # 1s for 30 steps: impossible
    assert sh._implausibly_fast(r30, now + 4) is True        # <5s: impossible
    assert sh._implausibly_fast(r30, now + 350) is False     # a real 30-step render
    assert sh._implausibly_fast({"ts": now, "params": {"steps": 4}}, now + 1) is False   # exempt
    assert sh._implausibly_fast({"ts": now, "params": {}}, now + 1) is False             # unknown steps


@pytest.fixture
def org_root(tmp_path, monkeypatch):
    """A per-org output dir that is BOTH the library root and the worklog dir, as in
    the pod layout (orgs/<org>/output)."""
    def out_dir(o=None):
        d = tmp_path / "orgs" / (o or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", out_dir)
    return out_dir


def _land(root: Path, rel: str, mtime: int):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n")
    os.utime(p, (mtime, mtime))


def test_finalize_landed_done_failed_and_stale(org_root):
    org = "acme"
    root = Path(org_root(org))
    for pid, prefix in (("fast", "fixes/fast"), ("slow", "fixes/slow"), ("stale", "fixes/stale")):
        sh.worklog.record(org, kind="fix", prompt="p", refs=[], uploads=[], parents=[],
                          output_prefix=prefix, params={"steps": 30}, pid=pid, status="queued")
    ts = {r["id"]: r["ts"] for r in sh.worklog.load(org)}
    _land(root, "fixes/fast_00001_.png", ts["fast"] + 1)      # landed 1s after dispatch
    _land(root, "fixes/slow_00001_.png", ts["slow"] + 350)    # a real render
    _land(root, "fixes/stale_00001_.png", ts["stale"] - 100)  # predates dispatch: sibling

    rows = sh.worklog.load(org)
    sh._finalize_landed(org, root, rows, int(time.time()) + 500)

    status = {r["id"]: r["status"] for r in sh.worklog.load(org)}
    assert status["fast"] == "failed"      # honesty guard: engine did not sample
    assert status["slow"] == "done"
    assert status["stale"] == "queued"     # pre-existing sibling, never delivered as this job
    failed = next(r for r in sh.worklog.load(org) if r["id"] == "fast")
    assert any("did not sample" in (e.get("note") or "") for e in failed.get("events", []))


# ── iteration hygiene: clean-ancestor fallback ────────────────────────────────────

def test_clean_base_falls_back_to_nearest_clean_ancestor(org_root):
    org = "acme"
    root = Path(org_root(org))
    # lineage: root.png (approved) → fix1 (flagged) → fix2 (flagged, the chosen base)
    for rel in ("root.png", "fixes/root_00001_.png", "fixes/root_00001__00001_.png"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"\x89PNG\r\n")
    (root / "library.json").write_text(json.dumps({"assets": [
        {"path": "root.png", "status": "approved"},
        {"path": "fixes/root_00001_.png", "status": "flagged"},
        {"path": "fixes/root_00001__00001_.png", "status": "flagged"},
    ]}))
    sh.worklog.record(org, kind="fix", prompt="a", refs=[], uploads=[], parents=["root.png"],
                      output_prefix="fixes/root", pid="p1", status="done")
    sh.worklog.record(org, kind="fix", prompt="b", refs=[], uploads=[],
                      parents=["fixes/root_00001_.png"],
                      output_prefix="fixes/root_00001_", pid="p2", status="done")

    # the flagged fix2 falls back to the approved root (skipping the flagged fix1)
    base, substituted = sh._clean_base(org, root, "fixes/root_00001__00001_.png")
    assert base == "root.png" and substituted is True
    # a clean base is returned unchanged
    assert sh._clean_base(org, root, "root.png") == ("root.png", False)
    assert sh._asset_status(root, "fixes/root_00001_.png") == "flagged"


# ── Job identity: every dispatch owns a unique output namespace ──────────────────
def test_job_tag_is_short_hex_and_unique():
    a, b = sh._job_tag(), sh._job_tag()
    assert len(a) == 6 and int(a, 16) >= 0
    assert a != b


# ── View/pose-change routing: repose asks use a free latent, anchored fixes keep
#    the encoded-base latent — and the prompt tail never contradicts the ask ─────
def test_fix_graph_anchored_default_keeps_pose():
    g = sh._fix_graph("x.png", "fix the coloring", "fixes/x")
    assert g["10"]["class_type"] == "VAEEncode"
    assert "same model, pose, framing" in g["6"]["inputs"]["prompt"]


def test_fix_graph_repose_uses_empty_latent_and_frees_pose():
    g = sh._fix_graph("x.png", "same outfit but from the side only", "fixes/x", size=(1024, 1536))
    assert g["10"]["class_type"] == "EmptySD3LatentImage"
    assert g["6"]["inputs"]["image1"] == ["1", 0]      # RAW reference, no scale mismatch
    assert g["13"]["inputs"]["model"] == ["11", 0]     # no CFGNorm on the free-latent path
    assert g["10"]["inputs"]["width"] == 1024 and g["10"]["inputs"]["height"] == 1536
    p = g["6"]["inputs"]["prompt"]
    assert "pose, framing" not in p          # must not pin the pose it was asked to change
    assert "view or pose" in p


def test_repose_lexicon_matches_view_changes_only():
    hits = ["show me the same model but from the side only", "turn around",
            "back view please", "make her facing left", "different angle"]
    misses = ["fix the coloring", "brighten the skin slightly", "remove the second strap"]
    for t in hits:
        assert sh._REPOSE.search(t), t
    for t in misses:
        assert not sh._REPOSE.search(t), t


# ── Flywheel retrieval: pointed-out mistakes condition the NEXT generation ──────
def test_mistakes_selects_corrections_not_praise():
    ratings = {
        "a.png": {"vote": -1, "notes": "sleeves way too wide"},
        "b.png": {"stars": 1, "notes": "color washed out"},
        "c.png": {"vote": 1, "notes": "love this lighting"},          # praise — excluded
        "d.png": {"notes": "strap should be thinner"},                 # bare note — a correction
        "e.png": {"vote": -1},                                         # no text — nothing to inject
    }
    out = sh._mistakes(ratings, ["a.png", "b.png", "c.png", "d.png", "e.png", "missing.png"])
    assert "sleeves way too wide" in out and "color washed out" in out
    assert "strap should be thinner" in out
    assert all("love this lighting" != m for m in out)
    assert len(out) <= 4


def test_fix_graph_injects_lessons_into_both_encoders():
    g = sh._fix_graph("x.png", "redo the sleeves", "fixes/x",
                      lessons=["sleeves way too wide", "color washed out"])
    pos, neg = g["6"]["inputs"]["prompt"], g["7"]["inputs"]["prompt"]
    assert "do not repeat" in pos and "sleeves way too wide" in pos
    assert "sleeves way too wide" in neg and "color washed out" in neg


def test_compose_graph_injects_lessons():
    g = sh._compose_graph(["x.png", "y.png"], "combine them", "composes/x",
                          lessons=["strap should be thinner"])
    assert "strap should be thinner" in g["6"]["inputs"]["prompt"]
    assert "strap should be thinner" in g["7"]["inputs"]["prompt"]
