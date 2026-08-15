"""
A hero film is generated, and the generated thing is a composition.

These hold the rules that were learned expensively and are now structural: the
frame size and duration reach the renderer, frame 0 is not black, the last frame
is the finished film, nothing is dimmer than the measured contrast floor, and
nothing but opacity and translateY is ever animated. Each of them is a thing that
renders SUCCESSFULLY when it is wrong, which is why they are asserted rather than
remembered.

Where a film is STORED and how it is authored is `documents_test` — a composition
is a `frame` document and this module owns no store. The render itself is a
subprocess against headless Chrome, exercised by `film_render_test.py`.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import doc_render, film  # noqa: E402

FILM = {
    "title": "Custody",
    "lead": "The credential your code never sees",
    "foot": "api.hanzo.ai/v1/cloud",
    "beats": [
        {"head": "Verified live", "note": "against the provider, before anything is stored"},
        {"head": "Sealed", "note": "in your org's own KMS namespace"},
        {"head": "Never returned", "note": "not in a response, an index, or a log line"},
    ],
}
ALT = "The credential is verified live, sealed in your org's own KMS namespace."


# ── the contract ──────────────────────────────────────────────────────────────

def test_duration_is_derived_from_the_beats():
    """Typing a duration beside a list of beats is a second copy of the list's
    length, and it is the copy that goes stale."""
    one = film.seconds([{"head": "a", "note": ""}])
    assert film.seconds(FILM["beats"]) == round(one + 2 * film._STEP, 1)
    assert film.seconds(FILM["beats"]) > film._HOLD   # the hold is inside it


def test_six_files_from_one_prefix():
    assert film.names("custody") == [
        "custody-tall.mp4", "custody-tall-first.jpg", "custody-tall-last.jpg",
        "custody-wide.mp4", "custody-wide-first.jpg", "custody-wide-last.jpg"]


def test_call_site_is_two_props():
    assert film.call_site("custody", ALT) == f'<Frame src="/custody" alt="{ALT}" />'


def test_a_quote_in_the_message_cannot_break_the_call_site():
    line = film.call_site("custody", 'She said "no"')
    assert '"' not in line[line.index("alt=") + 5:-4]


@pytest.mark.parametrize("typed,want", [
    ("Custody Hero", "custody-hero"), ("  CUSTODY  ", "custody"),
    ("a//b__c", "a-b-c"), ("...", ""), ("custody-hero", "custody-hero"),
])
def test_a_prefix_is_slugged_because_it_names_files(typed, want):
    assert film.slug(typed) == want


# ── the composition ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("shape", ["tall", "wide"])
def test_the_frame_size_reaches_the_renderer(shape):
    """Without these four attributes every master renders 1080x1920 portrait,
    successfully, and nothing reports it."""
    html = film.compose(FILM, shape)
    f = film.SHAPES[shape]
    assert f'data-width="{f["w"]}"' in html
    assert f'data-height="{f["h"]}"' in html
    assert f'data-fps="{film.FPS}"' in html
    assert f'data-duration="{film.seconds(FILM["beats"])}"' in html


@pytest.mark.parametrize("shape", ["tall", "wide"])
def test_a_generated_master_is_a_composition_the_archetype_can_read(shape):
    """The generator WRITES the frames contract and `doc_render` READS it. Two places
    encoding one contract drift silently — a master that renders is no evidence the
    reader still understands it — so the reader is pointed at the writer's own output."""
    declared = doc_render.composition(film.compose(FILM, shape))
    f = film.SHAPES[shape]
    assert declared["id"] == "main"
    assert (declared["width"], declared["height"]) == (f["w"], f["h"])
    assert declared["duration"] == film.seconds(FILM["beats"])


def test_the_two_shapes_are_not_the_same_document():
    """One master cannot be both a phone and a laptop, and hand-keeping two files
    that must say the same thing drifts on the first copy edit — so one generator
    writes both, and they differ only in shape."""
    tall, wide = film.compose(FILM, "tall"), film.compose(FILM, "wide")
    assert tall != wide
    for beat in FILM["beats"]:
        assert beat["head"] in tall and beat["head"] in wide
    assert 'data-width="1080"' in tall and 'data-width="1920"' in wide


def test_frame_zero_is_not_black():
    """Frame 0 is the poster a motion viewer stares at until the player takes
    over. The lead therefore carries no entrance: it is on screen at t=0."""
    html = film.compose(FILM, "wide")
    lead = html[html.index(".lead {"):html.index(".beat {")]
    assert "animation" not in lead
    assert "opacity: 0" not in lead
    assert FILM["lead"] in html


def test_the_film_ends_on_the_finished_thing():
    """The last frame is what every reduced-motion viewer is served instead of
    the film, so the last beat has to have landed with seconds to spare."""
    lands = film._OPEN + (len(FILM["beats"]) - 1) * film._STEP + film._RISE
    assert film.seconds(FILM["beats"]) - lands >= 3.0
    # `both` holds the end state after the animation finishes; without it every
    # beat snaps back to opacity 0 and the final frame is empty.
    assert "cubic-bezier(.16,1,.3,1) both" in film.compose(FILM, "wide")


def test_only_opacity_and_translate_are_animated():
    """The renderer REFUSES layout and text-reflow tweens — they snap to integer
    device pixels and stutter under seek-by-frame capture. There is one keyframe
    block and this is what is in it."""
    html = film.compose(FILM, "wide")
    frames = re.findall(r"@keyframes\s+\w+\s*\{(.*?)\}\s*\}", html, re.S)
    assert len(frames) == 1
    body = frames[0]
    assert set(re.findall(r"([a-z-]+)\s*:", body)) == {"opacity", "transform"}
    assert "translateY" in body
    for banned in ("left", "top:", "margin-left", "font-size", "letter-spacing", "width"):
        assert banned not in body


def test_nothing_is_dimmer_than_the_contrast_floor():
    """#777 is the dimmest grey that passes AA on true black; #6e6e6e fails."""
    for hexcolor in re.findall(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", film.compose(FILM, "wide")):
        h = hexcolor * 2 if len(hexcolor) == 3 else hexcolor
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        # greys only — the borders and the page are near-black by design, so this
        # asks about TEXT: anything above the page but below the floor is the bug.
        if r == g == b and 0x1f < r < 0x77:
            raise AssertionError(f"#{h} sits above the page and below the AA floor")


def test_the_words_stay_inside_the_crop():
    """`object-fit: cover` on a 390x844 phone leaves the middle 862px of the tall
    master. A word outside that column is a word nobody on a phone reads."""
    assert film.SHAPES["tall"]["col"] <= 862


def test_the_timeline_wait_is_opted_out_of():
    """This composition registers no window.__timelines entry, and without the
    renderer's own opt-out capture waits the full player timeout every run."""
    assert "data-no-timeline" in film.compose(FILM, "wide")


def test_the_clip_is_a_direct_child_of_the_root():
    """A visual clip that is not a direct child of the composition root is not
    scheduled, and one with no class="clip" is visible for the whole film."""
    root = film.compose(FILM, "wide")
    root = root[root.index('<div id="root"'):]
    assert re.match(r'<div id="root"[^>]*>\s*<section id="scene" class="clip"', root)


def test_the_words_are_escaped():
    html = film.compose({**FILM, "lead": "<script>x</script>"}, "wide")
    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_a_half_filled_film_still_composes():
    """The generator is a template's builder, and a template degrades rather than
    raising — every other one in the catalog does, and `prove` is what refuses
    something unrenderable."""
    html = film.compose({"lead": "Just a claim"}, "wide")
    assert doc_render.composition(html)["duration"] == film.seconds([])


def test_more_beats_than_a_film_holds_are_dropped():
    many = [{"head": f"beat {i}", "note": ""} for i in range(20)]
    html = film.compose({"lead": "x", "beats": many}, "wide")
    assert html.count('class="beat"') == film.BEATS


def test_tools_answers_honestly():
    """An empty value is the answer that lets a refusal name what is missing."""
    t = film.tools()
    assert set(t) == {"npx", "node", "ffmpeg", "ffprobe"}
    assert all(isinstance(v, str) for v in t.values())


def test_a_render_without_the_toolchain_refuses_and_names_it(monkeypatch, tmp_path):
    """Studio's own image carries no Node and no Chrome. A film asked for there
    must say so, never report a film it did not make."""
    monkeypatch.setattr(film, "tools",
                        lambda: {"npx": "", "node": "", "ffmpeg": "/f", "ffprobe": "/p"})
    with pytest.raises(film.FilmError) as e:
        film.render("<html></html>", tmp_path / "x", "hero-wide")
    assert "npx" in str(e.value) and "node" in str(e.value)


def test_a_stem_that_is_not_a_filename_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(film, "tools",
                        lambda: {k: "/x" for k in ("npx", "node", "ffmpeg", "ffprobe")})
    with pytest.raises(film.FilmError) as e:
        film.render("<html></html>", tmp_path / "x", "../../etc/passwd")
    assert "names files" in str(e.value)


# ── the served page ───────────────────────────────────────────────────────────

_HTML = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()


def test_a_film_has_no_surface_of_its_own():
    """A composition is a document. A second tab, a second store and a second
    authoring model for the same artifact is the thing this is not."""
    for gone in ('data-tab="films"', "loadFilms", "filmdlg", "/v1/films"):
        assert gone not in _HTML


def test_render_is_offered_on_a_composition_and_only_there():
    """`html` is already how this card knows a composition — the same signal the
    Play action reads, rather than a second field that can disagree with it."""
    assert "(d.formats||[]).includes('html')" in _HTML
    assert "renderDoc('${qq(d.id)}')" in _HTML
    assert "async function renderDoc(" in _HTML


def test_the_call_site_and_the_six_names_are_shown():
    """They are the contract: the files go in public/ under the prefix and the
    line goes in the page. Studio states both rather than describing them."""
    assert "d.frame" in _HTML and "d.names.join" in _HTML


def test_a_failed_render_is_shown_and_not_swallowed():
    assert "$('docderr').textContent=d.error" in _HTML


def test_a_dialog_is_centred():
    """`*{margin:0}` outranks the browser's own `dialog:modal{margin:auto}`, so
    without this every dialog Studio opens sits in the top-left corner. Measured
    at 1440x900: x=0, for all three of them."""
    css = _HTML[_HTML.index("dialog{background:"):]
    assert "margin:auto}" in css[:css.index("\n")]


def test_the_dialog_field_style_is_not_scoped_to_one_dialog():
    assert "#docdlg .docl{" not in _HTML
    assert "\n.docl{display:block" in _HTML
    assert "dialog input:not([type])" in _HTML


# ── The image renders what the code invokes ───────────────────────────────────

def test_the_image_installs_the_renderer_the_code_names():
    """The Dockerfile reads `RENDERER` out of film.py rather than repeating it,
    so the pin has one home. That coupling is a sed expression, which a
    reformatting of the assignment would silently stop matching — the build
    fails loudly on an empty pin, but only after a push, so it is asserted here
    where an edit is being made."""
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    assert "middleware/film.py" in dockerfile, "the image no longer reads the pin from the source"

    # The expression the image applies, asked of the source the image reads.
    src = (root / "middleware" / "film.py").read_text()
    found = re.findall(r'^RENDERER = "(.*)"$', src, re.M)
    assert found == [film.RENDERER], f"the image would install {found}, the code invokes {film.RENDERER}"


def test_the_renderer_pin_names_a_version():
    """`@hanzo/frames` unpinned would install whatever npm serves that day, and a
    film is supposed to render the same twice."""
    assert "@" in film.RENDERER.removeprefix("@"), f"{film.RENDERER} names no version"
