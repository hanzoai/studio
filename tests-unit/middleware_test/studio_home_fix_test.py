"""Unit tests for the fix pipeline (middleware/studio_home.py):

  * the fix/compose graphs name only models that exist on the worker AND carry none
    of the Flux-Kontext reference-method wiring that stippled Qwen edits;
  * the honesty guard flips an implausibly-fast completion to FAILED;
  * iteration hygiene falls the fix base back to the nearest clean ancestor.

Pure/filesystem only — no torch, no GPU, no network.
"""
import base64
import hashlib
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
    # LOW-denoise edit (default 0.25) of the SOURCE latent — never 1.0 (that is Regenerate).
    assert ks["inputs"]["denoise"] == sh.EDIT_DENOISE_DEFAULT == 0.25
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
    # A COMPLETE png (IEND tail): the integrity gate must judge these rows on
    # timing alone, not refuse them as truncated writes.
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32 + b"\x00\x00\x00\x00IEND\xaeB`\x82")
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
    p = g["6"]["inputs"]["prompt"]
    assert "pose, framing" in p                              # preservation directive
    assert "apply only the requested change" in p.lower()


def test_fix_graph_never_uses_empty_latent():
    """Edit is ALWAYS a low-denoise img2img of the source — a pose/view change is now
    REGENERATE, not Edit, so the fix graph NEVER falls back to EmptyLatentImage
    (the old repose auto-switch, which drifted the whole image, is gone)."""
    for instruction in ("fix the coloring", "same outfit but from the side only",
                        "make her standing and looking away"):
        g = sh._fix_graph("x.png", instruction, "fixes/x")
        assert not any("Empty" in n.get("class_type", "") for n in g.values()), instruction
        ks = next(n for n in g.values() if n["class_type"] == "KSampler")["inputs"]
        assert g[ks["latent_image"][0]]["class_type"] == "VAEEncode"
        assert sh._assert_edit_graph(g) == ""


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


def test_fix_graph_lessons_steer_negative_only():
    # Lessons ride ONLY the negative encoder. Putting "do not repeat: lifestyle
    # scene" in the POSITIVE handed the model the very tokens to avoid — diffusion
    # weighs tokens, not negation — which re-caused the drift being critiqued.
    g = sh._fix_graph("x.png", "redo the sleeves", "fixes/x",
                      lessons=["sleeves way too wide", "color washed out"])
    pos, neg = g["6"]["inputs"]["prompt"], g["7"]["inputs"]["prompt"]
    assert "sleeves way too wide" not in pos and "do not repeat" not in pos
    assert "sleeves way too wide" in neg and "color washed out" in neg


def test_compose_graph_injects_lessons():
    g = sh._compose_graph(["x.png", "y.png"], "combine them", "composes/x",
                          lessons=["strap should be thinner"])
    assert "strap should be thinner" not in g["6"]["inputs"]["prompt"]
    assert "strap should be thinner" in g["7"]["inputs"]["prompt"]


# ── Ingest upsert identity: a counter-suffixed landed file belongs to its
#    dispatch row — mirroring must never add a duplicate ingest row ────────────
def test_has_output_matches_counter_suffixed_files(tmp_path, monkeypatch):
    import middleware.worklog as wl
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory", lambda org: str(tmp_path))
    wl.record("acme", kind="fix", prompt="p", refs=[], uploads=[], parents=[],
              output_prefix="fixes/x_ab12cd")
    assert wl.has_output("acme", "fixes/x_ab12cd_00001_.png")      # the row owns its landed file
    assert wl.has_output("acme", "fixes/x_ab12cd")                 # exact prefix still matches
    assert not wl.has_output("acme", "fixes/other_00001_.png")     # unrelated file → new row ok


# ── Job identity for re-run and template-render (the phantom-image class) ────────
def test_retag_points_every_saveimage_at_unique_prefix():
    g = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}},
         "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fixes/original"}},
         "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fixes/original"}}}
    sh._retag(g, "reruns/original_ab12cd")
    assert g["9"]["inputs"]["filename_prefix"] == "reruns/original_ab12cd"
    assert g["10"]["inputs"]["filename_prefix"] == "reruns/original_ab12cd"
    # a re-run graph never keeps the source's SaveImage prefix (that was the collision)
    assert all(n.get("inputs", {}).get("filename_prefix") != "fixes/original"
               for n in g.values() if n.get("class_type") == "SaveImage")


# ── State isolation & job construction ────────────────────────────────────────────
#    An Edit is built from the SELECTED source alone; two concurrent edits of DIFFERENT
#    sources can never share a staged file, a LoadImage ref, a positive prompt, a
#    sampling latent or a parent. Content-addressed staging (name == sha256 of the
#    bytes) is the binding proof: the worker's LoadImage resolves the exact selected
#    image, and different sources are cryptographically un-collidable on a shared
#    worker input dir — the "unrelated random picture / wrong image" class of bug.
from middleware import gpu_dispatch as gd


def _pos_prompt(g):
    """The positive prompt actually wired into KSampler (not read by node id)."""
    ks = next(n for n in g.values() if n["class_type"] == "KSampler")
    return g[ks["inputs"]["positive"][0]]["inputs"]["prompt"]


def _load_ref(g):
    return next(n for n in g.values() if n["class_type"] == "LoadImage")["inputs"]["image"]


def _save_prefix(g):
    return next(n for n in g.values() if n["class_type"] in sh._SAVERS)["inputs"]["filename_prefix"]


@pytest.fixture
def in_dir(tmp_path, monkeypatch):
    """Per-org input dir (orgs/<org>/input) where sources are staged for the worker —
    patched on BOTH modules that touch it: studio_home stages, gpu_dispatch inlines."""
    def _d(o=None):
        d = tmp_path / "orgs" / (o or "default") / "input"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(sh.folder_paths, "get_org_input_directory", _d)
    monkeypatch.setattr(gd.folder_paths, "get_org_input_directory", _d)
    return _d


def test_stage_source_is_content_addressed(in_dir, tmp_path):
    d = Path(in_dir("acme"))
    a = tmp_path / "a.png"
    a.write_bytes(b"AAAA-dress-bytes")
    b = tmp_path / "b.png"
    b.write_bytes(b"BBBB-clasp-bytes")
    na, nb = sh._stage_source(d, a, "fix"), sh._stage_source(d, b, "fix")
    # different bytes -> different name (cryptographic, not a salted-hash coincidence)
    assert na != nb
    # the name IS the sha256 of the bytes: a verifiable binding to the EXACT source
    assert na == f"fix_src_{hashlib.sha256(a.read_bytes()).hexdigest()[:16]}.png"
    assert (d / na).read_bytes() == a.read_bytes()          # staged bytes are the source
    assert sh._stage_source(d, a, "fix") == na              # idempotent: same source, same name
    a.write_bytes(b"AAAA-dress-bytez")                      # one byte changes the address
    assert sh._stage_source(d, a, "fix") != na


def test_two_concurrent_edits_are_isolated(in_dir, tmp_path):
    """The reported scenario: Job A (design A, 'make the dress red') and Job B
    (design B, 'add a silver clasp') constructed back-to-back share NOTHING."""
    d = Path(in_dir("acme"))
    srcA = tmp_path / "A.png"
    srcA.write_bytes(b"\x89PNG-design-A" + b"\x00" * 64)
    srcB = tmp_path / "B.png"
    srcB.write_bytes(b"\x89PNG-design-B" + b"\x00" * 64)
    refA, refB = sh._stage_source(d, srcA, "fix"), sh._stage_source(d, srcB, "fix")
    gA = sh._fix_graph(refA, "make the dress red", "fixes/A_" + sh._job_tag())
    gB = sh._fix_graph(refB, "add a silver clasp", "fixes/B_" + sh._job_tag())
    # 1) distinct staged sources, each graph LoadImages its OWN source
    assert refA != refB
    assert _load_ref(gA) == refA and _load_ref(gB) == refB
    # 2) prompts do not bleed across jobs
    assert "make the dress red" in _pos_prompt(gA) and "clasp" not in _pos_prompt(gA)
    assert "add a silver clasp" in _pos_prompt(gB) and "dress red" not in _pos_prompt(gB)
    # 3) both are true low-denoise edits of their own source — no text-to-image fallback
    assert sh._assert_edit_graph(gA) == "" and sh._assert_edit_graph(gB) == ""
    for g in (gA, gB):
        ks = next(n for n in g.values() if n["class_type"] == "KSampler")
        assert g[ks["inputs"]["latent_image"][0]]["class_type"] == "VAEEncode"
    # 4) distinct output namespaces (no phantom-image cross-claim)
    assert _save_prefix(gA) != _save_prefix(gB)


def test_collect_input_images_ships_exact_source_and_is_org_scoped(in_dir, tmp_path):
    """gpu_dispatch inlines the bytes the worker will LoadImage. It must ship the
    SELECTED org's copy exactly — never a same-named file from another org."""
    dA, dB = Path(in_dir("acme")), Path(in_dir("other"))
    src = tmp_path / "s.png"
    src.write_bytes(b"\x89PNG-real-source" + b"\x01" * 40)
    ref = sh._stage_source(dA, src, "fix")
    (dB / ref).write_bytes(b"\x89PNG-WRONG-org" + b"\x02" * 40)   # decoy, same basename
    shipped = gd._collect_input_images(sh._fix_graph(ref, "brighten", "fixes/x"), "acme")
    got = {s["name"]: base64.b64decode(s["data"]) for s in shipped}
    assert ref in got and got[ref] == src.read_bytes()           # acme's exact bytes
    assert b"WRONG-org" not in got[ref]


def test_assert_edit_graph_rejects_text_to_image_fallback():
    """The hard invariant: an Edit can NEVER be silently served by a text-to-image
    (EmptyLatentImage) graph. If source resolution ever produced one, the assert
    REJECTS it — dispatch raises, never renders an unrelated picture."""
    t2i = {
        "1": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024}},
        "2": {"class_type": "KSampler", "inputs": {"latent_image": ["1", 0], "denoise": 1.0}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "x"}},
    }
    reason = sh._assert_edit_graph(t2i)
    assert reason and "EmptyLatentImage" in reason                # rejected with a clear cause
