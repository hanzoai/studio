"""The composer's default create path — text-to-image — and the UX invariants the live
review demanded. Two concerns:

  * backend  — _image_graph is a REAL text-to-image graph (EmptySD3LatentImage → SaveImage,
               the Z-Image-Turbo recipe), and dispatch_image validates before any dispatch.
  * frontend — the shipped page defaults the composer to an IMAGE model, routes text→image
               (no "attach a photo first" dead end), ALWAYS shows a generating state on
               Create, puts Download first-class on the tile AND the viewer, and stops
               badging active work as ARCHIVED. Asserted statically against the served HTML
               so the intuitiveness fixes can't silently regress.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import studio_home as sh

_HTML = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()


# ── backend: the text-to-image graph ────────────────────────────────────────────────
def test_image_graph_is_a_real_text_to_image_create():
    g = sh._image_graph("a poster for a jazz night", "image/test")
    classes = {n["class_type"] for n in g.values()}
    # a true text-to-image CREATE starts from an empty latent (not an edit of a source)…
    assert "EmptySD3LatentImage" in classes
    # …and produces a visible saved image through the full load→sample→decode→save chain.
    assert {"UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "KSampler",
            "VAEDecode", "SaveImage"} <= classes
    # the user's words land in the positive prompt (node 5), negative (node 6) stays empty.
    assert "jazz" in g["5"]["inputs"]["text"]
    assert g["6"]["inputs"]["text"] == ""
    # the exact model set the shipped Z-Image-Turbo blueprint names.
    assert g["1"]["inputs"]["unet_name"] == sh._ZIMAGE_UNET
    assert g["2"]["inputs"]["clip_name"] == sh._ZIMAGE_CLIP and g["2"]["inputs"]["type"] == "lumina2"
    assert g["3"]["inputs"]["vae_name"] == sh._ZIMAGE_VAE
    # turbo recipe: 4 steps, cfg 1.0, res_multistep/simple, denoise 1.0 (a fresh render).
    ks = next(n for n in g.values() if n["class_type"] == "KSampler")["inputs"]
    assert ks["steps"] == 4 and ks["cfg"] == 1.0 and ks["denoise"] == 1.0
    assert ks["sampler_name"] == "res_multistep" and ks["scheduler"] == "simple"


def test_image_graph_dims_clamp_and_snap():
    g = sh._image_graph("x" * 5, "p", width=1000, height=770)
    lat = g["7"]["inputs"]
    assert lat["width"] % 16 == 0 and lat["height"] % 16 == 0            # snapped to the 16-px grid
    g2 = sh._image_graph("x" * 5, "p", width=50, height=9000)
    assert 256 <= g2["7"]["inputs"]["width"] <= 2048                     # clamped low
    assert 256 <= g2["7"]["inputs"]["height"] <= 2048                    # clamped high


def test_dispatch_image_rejects_bad_prompt_before_dispatch():
    # A too-short/empty prompt fails fast with a human reason — never a silent nothing,
    # never a network call. (No server/GPU needed: the guard is the first statement.)
    for bad in ("", "hi", "  "):
        with pytest.raises(sh.DispatchError):
            asyncio.run(sh.dispatch_image(None, None, "org", bad))


# ── frontend: the served page's intuitiveness invariants ─────────────────────────────
def test_composer_defaults_to_an_image_model():
    # default is Zen Image (an IMAGE model), NOT a chat model, so the first ↑ produces
    # something visible. Decoupled from the chat model (its own storage key).
    assert "hz_create_model||'zen-image'" in _HTML
    assert "hz_create_model" in _HTML and "localStorage.hz_model=v" not in _HTML
    # the Image group leads the picker; the code model is hidden from the creative composer.
    assert _HTML.index("{label:'Image'") < _HTML.index("{label:'Assistant'")
    assert "zen5-coder" not in _HTML


def test_text_only_prompt_routes_to_text_to_image():
    # the DEFAULT create (words, no photo) hits the new /v1/library/image route — the old
    # "attach a photo first" dead end is gone.
    assert "url:'/v1/library/image'" in _HTML
    assert "Describe what to create, then attach a photo with +" not in _HTML


def test_create_always_shows_a_state_and_fails_friendly():
    assert "function pulseGenerating(" in _HTML and "pulseGenerating(plan)" in _HTML
    assert "function createFailed(" in _HTML and "tap ↑ to retry" in _HTML


def test_download_is_first_class_on_tile_and_viewer():
    assert "function downloadImage(" in _HTML and "function dlMenu(" in _HTML
    assert "dlMenu(event,'" in _HTML          # a per-tile download control
    assert 'id="zrdl"' in _HTML                # a download control in the image viewer
    # PNG/JPG/WebP format choice is exposed at download.
    assert "downloadImage(path,'jpg')" in _HTML or "downloadImage(path,v)" in _HTML


def test_active_items_are_not_badged_archived():
    # the old unconditional status badge (which read every active item as its status /
    # "archived") is gone; a badge now renders ONLY for deleted/flagged items.
    assert "'archived':a.status}" not in _HTML
    assert "a.status==='deleted'||a.status==='flagged'" in _HTML


def test_bars_are_opaque_and_dev_control_is_gated():
    assert "background:var(--bg);z-index:30" in _HTML     # opaque fixed header
    assert "#livebar::before" in _HTML                    # full-bleed opaque backing behind the sticky status bar
    assert "rgba(10,10,11,.82)" not in _HTML              # the old translucent header is gone
    assert "body:not(.hzdev) .pico.dev" in _HTML          # </> hidden unless Developers mode
    assert 'class="pico dev"' in _HTML


def test_mobile_collapses_the_filter_pills():
    assert ".chips{flex-wrap:nowrap;overflow-x:auto" in _HTML


# ── RED create-lane regressions (@c00384cf): F1 dedup identity, F2 dim coercion ──

def test_dedup_key_is_stable_identity_not_the_random_prefix():
    # F1: the key must be the STABLE render identity (org|kind|prompt|refs|uploads) —
    # NOT output_prefix, which each lane stamps with a random _job_tag() so two
    # identical submits used to miss and double-bill.
    k = sh._dedup_key("acme", "image", "a jazz poster", [], [])
    assert k == sh._dedup_key("acme", "image", "a jazz poster", [], []), "identical creates share a key"
    assert k != sh._dedup_key("acme", "image", "a DIFFERENT poster", [], []), "different prompt → different key"
    assert k != sh._dedup_key("other", "image", "a jazz poster", [], []), "different org → different key"
    assert k != sh._dedup_key("acme", "video", "a jazz poster", [], []), "different lane → different key"
    assert k != sh._dedup_key("acme", "image", "a jazz poster", ["ref.png"], []), "different refs → different key"


def test_image_and_video_graphs_reject_non_numeric_dims():
    # F2: a non-numeric width/height/length raises DispatchError (→ clean 400), never a bare ValueError (→ 500).
    for bad in ("abc", "10px", [], {}):
        with pytest.raises(sh.DispatchError):
            sh._image_graph("a valid prompt", "image/x", width=bad)
        with pytest.raises(sh.DispatchError):
            sh._image_graph("a valid prompt", "image/x", height=bad)
        with pytest.raises(sh.DispatchError):
            sh._video_graph("a valid prompt", "video/x", width=bad)
    # numeric strings still coerce and render
    g = sh._image_graph("a valid prompt", "image/x", width="512", height="512")
    assert g["7"]["inputs"]["width"] == 512 and g["7"]["inputs"]["height"] == 512
