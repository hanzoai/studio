"""
The render itself: two masters and four stills, in headless Chrome.

This is the only test that leaves the process. It skips where the toolchain is
absent — which is Studio's own image, the light CI venv, and any model-less pod —
because a film needs Node, the renderer's Chrome and ffmpeg, and pretending
otherwise is how a green suite comes to certify a lane that cannot run.

What it proves is the thing reading cannot: that the sizes written into the
composition are the sizes that come out. A master that renders 1080x1920 when it
meant to be 1920x1080 renders SUCCESSFULLY, and the only way to know is to
measure the file.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import film  # noqa: E402

pytestmark = pytest.mark.skipif(
    any(not p for p in film.tools().values()),
    reason="a film needs npx, node, ffmpeg and ffprobe: " + repr(film.tools()))

FILM = {
    "title": "Custody", "src": "custody",
    "alt": "The credential is verified live and never returned.",
    "lead": "The credential your code never sees",
    "foot": "api.hanzo.ai/v1/cloud",
    "beats": [{"head": "Verified live", "note": "before anything is stored"},
              {"head": "Sealed", "note": "in your org's own KMS namespace"}],
}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return film.render(FILM, tmp_path_factory.mktemp("film"))


def test_all_six_files_are_there(rendered):
    assert sorted(rendered["files"]) == sorted(film.names("custody"))
    for name, p in rendered["files"].items():
        assert p.is_file() and p.stat().st_size > 0, name


def test_each_master_is_the_shape_it_was_composed_for(rendered):
    for shape, m in rendered["measured"].items():
        assert (m["width"], m["height"]) == (film.SHAPES[shape]["w"], film.SHAPES[shape]["h"])


def test_the_master_is_as_long_as_the_beats_say(rendered):
    want = film.seconds(FILM["beats"])
    for shape, m in rendered["measured"].items():
        assert abs(m["seconds"] - want) < 0.1, (shape, m["seconds"], want)


def test_the_poster_is_not_a_black_rectangle(rendered):
    """Frame 0 is what a motion viewer sees until the player takes over. A JPEG
    of a flat black frame compresses to nearly nothing; one with the lead on it
    does not."""
    for shape in film.SHAPES:
        first = rendered["files"][f"custody-{shape}-first.jpg"]
        assert first.stat().st_size > 6_000, f"{shape} poster is {first.stat().st_size}B"


def test_the_last_frame_carries_more_than_the_first(rendered):
    """The film ends on the finished thing — every beat landed and held — and
    that final frame is the whole film for a reduced-motion viewer."""
    for shape in film.SHAPES:
        first = rendered["files"][f"custody-{shape}-first.jpg"].stat().st_size
        last = rendered["files"][f"custody-{shape}-last.jpg"].stat().st_size
        assert last > first, f"{shape}: last {last}B is not richer than first {first}B"


def test_the_renderer_accepts_the_composition_under_strict(rendered):
    """`--strict` makes the renderer's lint fatal, so reaching a file at all is
    the proof that no rule it enforces was broken — the layout-tween refusal
    first among them."""
    assert rendered["files"]["custody-wide.mp4"].stat().st_size > 10_000


def test_a_second_render_is_the_same_film(tmp_path):
    """Deterministic by construction: no clock, no unseeded randomness, no
    network. Two renders of one composition are the same bytes."""
    import hashlib
    a = film.render(FILM, tmp_path / "a")["files"]["custody-wide.mp4"]
    b = film.render(FILM, tmp_path / "b")["files"]["custody-wide.mp4"]
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest(a) == digest(b)


def test_a_film_renders_without_the_design_checkout(tmp_path, monkeypatch):
    """Geist is the only face a Hanzo surface draws, and an image that shipped
    only Studio has no copy of it. The composition falls through its own font
    stack rather than rendering nothing."""
    monkeypatch.setattr(film.design, "faces", dict)
    out = film.render(FILM, tmp_path / "bare")
    assert not (tmp_path / "bare" / "wide" / "assets" / "geist.woff2").exists()
    assert out["measured"]["wide"]["width"] == 1920
    assert out["files"]["custody-wide-first.jpg"].stat().st_size > 6_000


def test_a_composition_the_renderer_refuses_is_a_refusal_here(tmp_path, monkeypatch):
    """Not a silent mp4 of the wrong thing: `--strict` turns a lint error into a
    non-zero exit, and the driver carries the renderer's own words out."""
    monkeypatch.setattr(film, "compose", lambda f, s: "<!doctype html><html><body>no root</body></html>")
    with pytest.raises(film.FilmError) as e:
        film.render(FILM, tmp_path / "broken")
    assert "npx" in str(e.value)
