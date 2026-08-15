"""
A film is its beats, and the composition is a derivation.

These hold the rules that were learned expensively and are now structural: the
frame size and duration reach the renderer, frame 0 is not black, the last frame
is the finished film, nothing is dimmer than the measured contrast floor, and
nothing but opacity and translateY is ever animated. Each of them is a thing that
renders SUCCESSFULLY when it is wrong, which is why they are asserted rather than
remembered.

The render itself is a subprocess against headless Chrome. It is exercised by
`film_render_test.py`, which skips where the toolchain is absent.
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import folder_paths  # noqa: E402
from middleware import film  # noqa: E402

FILM = {
    "title": "Custody",
    "src": "custody",
    "alt": "The credential is verified live, sealed in your org's own KMS namespace, and never returned.",
    "lead": "The credential your code never sees",
    "foot": "api.hanzo.ai/v1/cloud",
    "beats": [
        {"head": "Verified live", "note": "against the provider, before anything is stored"},
        {"head": "Sealed", "note": "in your org's own KMS namespace"},
        {"head": "Never returned", "note": "not in a response, an index, or a log line"},
    ],
}


# ── the value ─────────────────────────────────────────────────────────────────

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
    assert film.call_site(FILM) == f'<Frame src="/custody" alt="{FILM["alt"]}" />'


def test_a_quote_in_the_message_cannot_break_the_call_site():
    line = film.call_site({**FILM, "alt": 'She said "no"'})
    assert '"' not in line[line.index("alt=") + 5:-4]


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
    from middleware import doc_render

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
    last_beat_lands = film._OPEN + (len(FILM["beats"]) - 1) * film._STEP + film._RISE
    assert film.seconds(FILM["beats"]) - last_beat_lands >= 3.0
    # `both` holds the end state after the animation finishes; without it every
    # beat snaps back to opacity 0 and the final frame is empty.
    assert "animation: rise" in film.compose(FILM, "wide")
    assert "cubic-bezier(.16,1,.3,1) both" in film.compose(FILM, "wide")


def test_only_opacity_and_translate_are_animated():
    """The renderer REFUSES layout and text-reflow tweens — they snap to integer
    device pixels and stutter under seek-by-frame capture. There is one keyframe
    block and this is what is in it."""
    html = film.compose(FILM, "wide")
    frames = re.findall(r"@keyframes\s+\w+\s*\{(.*?)\}\s*\}", html, re.S)
    assert len(frames) == 1
    body = frames[0]
    props = set(re.findall(r"([a-z-]+)\s*:", body))
    assert props == {"opacity", "transform"}
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
    col = film.SHAPES["tall"]["col"]
    assert col <= 862


def test_the_timeline_wait_is_opted_out_of():
    """This composition registers no window.__timelines entry, and without the
    renderer's own opt-out capture waits the full 45s player timeout every run."""
    assert "data-no-timeline" in film.compose(FILM, "wide")


def test_the_clip_is_a_direct_child_of_the_root():
    """A visual clip that is not a direct child of the composition root is not
    scheduled, and one with no class="clip" is visible for the whole film."""
    html = film.compose(FILM, "wide")
    root = html[html.index('<div id="root"'):]
    assert re.match(r'<div id="root"[^>]*>\s*<section id="scene" class="clip"', root)


def test_the_words_are_escaped():
    html = film.compose({**FILM, "lead": '<script>x</script>'}, "wide")
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


# ── reading a film ────────────────────────────────────────────────────────────

def test_read_accepts_a_film():
    assert film.read(FILM)["src"] == "custody"


@pytest.mark.parametrize("bad,why", [
    ({**FILM, "src": "Custody Hero"}, "src names six files"),
    ({**FILM, "src": ""}, "src names six files"),
    ({**FILM, "alt": "  "}, "alt is the film's own words"),
    ({**FILM, "beats": []}, "at least one beat"),
    ({**FILM, "beats": [{"note": "x"}]}, "no head"),
    ({**FILM, "beats": [{"head": "a", "note": ""}] * 9}, "at most"),
])
def test_read_refuses_and_says_why(bad, why):
    with pytest.raises(film.FilmError) as e:
        film.read(bad)
    assert why.split()[0] in str(e.value)


def test_tools_answers_honestly():
    """An empty value is the answer that lets a refusal name what is missing."""
    t = film.tools()
    assert set(t) == {"npx", "node", "ffmpeg", "ffprobe"}
    assert all(isinstance(v, str) for v in t.values())


# ── the REST surface, on a real app ───────────────────────────────────────────

class _Sync:
    def __init__(self, c, loop):
        self.c, self.loop = c, loop

    def __getattr__(self, verb):
        def call(path, **kw):
            r = self.loop.run_until_complete(getattr(self.c, verb)(path, **kw))
            body = self.loop.run_until_complete(
                r.json() if "json" in r.headers.get("Content-Type", "") else r.text())
            return r.status, body
        return call


@pytest.fixture
def client(tmp_path, monkeypatch):
    def outd(org=None):
        d = tmp_path / "orgs" / (org or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(folder_paths, "get_org_output_directory", outd)

    app = web.Application()
    routes = web.RouteTableDef()
    film.add_film_routes(routes, object(),
                         org_of=lambda r: r.headers.get("X-Org", "default"),
                         owner_of=lambda r: r.headers.get("X-User", ""))
    app.add_routes(routes)
    loop = asyncio.new_event_loop()
    tc = TestClient(TestServer(app), loop=loop)
    loop.run_until_complete(tc.start_server())
    try:
        yield _Sync(tc, loop)
    finally:
        loop.run_until_complete(tc.close())
        loop.close()


def test_author_read_edit_delete(client):
    s, doc = client.post("/v1/films", json=FILM)
    assert s == 201, doc
    fid = doc["id"]
    assert doc["frame"] == film.call_site(FILM)
    assert doc["names"] == film.names("custody")
    assert doc["seconds"] == film.seconds(FILM["beats"])

    s, got = client.get(f"/v1/films/{fid}")
    assert s == 200 and got["film"]["lead"] == FILM["lead"]

    s, ed = client.patch(f"/v1/films/{fid}", json={"lead": "Held, never handed over"})
    assert s == 200 and ed["film"]["lead"] == "Held, never handed over"
    assert ed["film"]["beats"] == FILM["beats"]      # a patch is an edit, not a replace

    s, idx = client.get("/v1/films")
    assert s == 200 and [f["id"] for f in idx["films"]] == [fid]

    assert client.delete(f"/v1/films/{fid}")[0] == 200
    assert client.get(f"/v1/films/{fid}")[0] == 404


def test_an_edit_is_still_held_to_the_rules(client):
    fid = client.post("/v1/films", json=FILM)[1]["id"]
    s, err = client.patch(f"/v1/films/{fid}", json={"beats": []})
    assert s == 400 and "beat" in err["error"]
    assert client.get(f"/v1/films/{fid}")[1]["film"]["beats"] == FILM["beats"]


def test_the_composition_is_inspectable(client):
    fid = client.post("/v1/films", json=FILM)[1]["id"]
    for shape in ("tall", "wide"):
        s, html = client.get(f"/v1/films/{fid}/composition?shape={shape}")
        assert s == 200 and html == film.compose(FILM, shape)
    assert client.get(f"/v1/films/{fid}/composition?shape=square")[0] == 400


def test_one_org_never_sees_another(client):
    fid = client.post("/v1/films", json=FILM, headers={"X-Org": "a"})[1]["id"]
    assert client.get(f"/v1/films/{fid}", headers={"X-Org": "b"})[0] == 404
    assert client.get("/v1/films", headers={"X-Org": "b"})[1]["films"] == []


def test_a_refused_film_stores_nothing(client):
    assert client.post("/v1/films", json={**FILM, "src": "Not A Slug"})[0] == 400
    assert client.get("/v1/films")[1]["films"] == []


def test_render_lands_the_six_files_through_the_one_ingest(client, monkeypatch, tmp_path):
    """Studio's library has ONE door, and a film's output goes through it like
    every other render — so the six files are gallery assets, not a private pile."""
    seen = []
    monkeypatch.setattr(film, "tools", lambda: {t: "/x" for t in ("npx", "node", "ffmpeg", "ffprobe")})
    monkeypatch.setattr(film, "render", lambda f, w: {
        "files": {n: tmp_path / n for n in film.names(f["src"])},
        "measured": {"tall": {"width": 1080, "height": 1920, "seconds": 5.2},
                     "wide": {"width": 1920, "height": 1080, "seconds": 5.2}}})

    app_routes = web.RouteTableDef()
    film.add_film_routes(app_routes, object(), org_of=lambda r: "default",
                         land=lambda org, rel, src: seen.append(rel))
    app = web.Application()
    app.add_routes(app_routes)
    loop = asyncio.new_event_loop()
    tc = TestClient(TestServer(app), loop=loop)
    loop.run_until_complete(tc.start_server())
    try:
        c = _Sync(tc, loop)
        fid = c.post("/v1/films", json=FILM)[1]["id"]
        s, doc = c.post(f"/v1/films/{fid}/render")
        assert s == 200, doc
        assert seen == [f"film/{n}" for n in film.names("custody")]
        assert doc["rendered"] == seen
        assert doc["measured"]["wide"]["width"] == 1920
    finally:
        loop.run_until_complete(tc.close())
        loop.close()


def test_render_without_the_toolchain_refuses_and_names_it(client, monkeypatch):
    """Studio's own image carries no Node and no Chrome. A film asked for there
    must say so, never report a film it did not make."""
    monkeypatch.setattr(film, "tools",
                        lambda: {"npx": "", "node": "", "ffmpeg": "/f", "ffprobe": "/p"})
    fid = client.post("/v1/films", json=FILM)[1]["id"]
    s, err = client.post(f"/v1/films/{fid}/render")
    assert s == 503
    assert "npx" in err["error"] and "node" in err["error"]
    assert client.get(f"/v1/films/{fid}")[1]["rendered"] == []


# ── the served page ───────────────────────────────────────────────────────────

_HTML = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()


def test_films_is_a_tab_that_shows_its_own_section():
    """Three edits make a tab, and missing the array is a button that hides
    every panel including its own."""
    assert """<button data-tab="films" onclick="tab('films')\"""" in _HTML
    assert '<section id="films" hidden>' in _HTML
    assert "'assets','documents','films','templates'" in _HTML
    assert "if(name==='films')loadFilms();" in _HTML


def test_render_is_not_offered_where_it_cannot_run():
    """A button that always fails is worse than no button. The tab reads the
    toolchain the API reports and says what is missing instead."""
    assert "const miss=Object.entries(FILMS.tools||{}).filter(([,p])=>!p)" in _HTML
    assert "can?`<button class=\"mini hot\" onclick=\"makeFilm(" in _HTML


def test_the_form_has_no_knob_for_a_derived_thing():
    """Length, frame size and fps come from the beats and the shape. A field for
    one of them is a second copy of an answer the film already gives."""
    form = _HTML[_HTML.index('<dialog id="filmdlg"'):_HTML.index("</dialog>", _HTML.index('<dialog id="filmdlg"'))]
    for knob in ("fm_fps", "fm_duration", "fm_width", "fm_height", "fm_seconds"):
        assert knob not in form


def test_the_call_site_and_the_six_names_are_shown():
    """They are the contract: the files go in public/ under the prefix and the
    line goes in the page. Studio states both rather than describing them."""
    assert "function showFilm(" in _HTML
    assert "d.frame" in _HTML and "d.names.join" in _HTML


def test_the_dialog_field_style_is_not_scoped_to_one_dialog():
    assert "#docdlg .docl{" not in _HTML
    assert "\n.docl{display:block" in _HTML
    assert "dialog input:not([type])" in _HTML


def test_a_dialog_is_centred():
    """`*{margin:0}` outranks the browser's own `dialog:modal{margin:auto}`, so
    without this every dialog Studio opens sits in the top-left corner. Measured
    at 1440x900: 0px, for all three of them."""
    css = _HTML[_HTML.index("dialog{background:"):]
    assert "margin:auto}" in css[:css.index("\n")]
