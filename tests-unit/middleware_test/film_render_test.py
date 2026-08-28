"""
The render itself: a master and its two stills, in headless Chrome.

This is the only test that leaves the process. It skips where the toolchain is
absent — which is Studio's own image, the light CI venv, and any model-less pod —
because a composition needs Node, the renderer's Chrome and ffmpeg, and pretending
otherwise is how a green suite comes to certify a lane that cannot run.

What it proves is the thing reading cannot: that the size written into the
composition is the size that comes out. A master that renders 1080x1920 when it
meant to be 1920x1080 renders SUCCESSFULLY, and the only way to know is to measure
the file.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import film  # noqa: E402

pytestmark = pytest.mark.skipif(
    any(not p for p in film.tools().values()),
    reason="a composition needs npx, node, ffmpeg and ffprobe: " + repr(film.tools()))

FILM = {
    "title": "Custody",
    "lead": "The credential your code never sees",
    "foot": "api.hanzo.ai/v1/cloud",
    "beats": [{"head": "Verified live", "note": "before anything is stored"},
              {"head": "Sealed", "note": "in your org's own KMS namespace"}],
}


@pytest.fixture(scope="module")
def both(tmp_path_factory):
    """Both shapes, once — this is what a page needs and what the six files are."""
    root = tmp_path_factory.mktemp("film")
    return {shape: film.render(film.compose(FILM, shape), root / shape, f"custody-{shape}")
            for shape in film.SHAPES}


def test_the_six_files_are_there(both):
    made = sorted(n for out in both.values() for n in out["files"])
    assert made == sorted(film.names("custody"))
    for out in both.values():
        for name, p in out["files"].items():
            assert p.is_file() and p.stat().st_size > 0, name


def test_each_master_is_the_shape_it_was_composed_for(both):
    for shape, out in both.items():
        m = out["measured"]
        assert (m["width"], m["height"]) == (film.SHAPES[shape]["w"], film.SHAPES[shape]["h"])


def test_the_master_is_as_long_as_the_beats_say(both):
    want = film.seconds(FILM["beats"])
    for shape, out in both.items():
        assert abs(out["measured"]["seconds"] - want) < 0.1, (shape, out["measured"])


def test_the_poster_is_not_a_black_rectangle(both):
    """Frame 0 is what a motion viewer sees until the player takes over. A JPEG of
    a flat black frame compresses to nearly nothing; one with the lead on it does
    not."""
    for shape, out in both.items():
        first = out["files"][f"custody-{shape}-first.jpg"]
        assert first.stat().st_size > 6_000, f"{shape} poster is {first.stat().st_size}B"


def test_the_last_frame_carries_more_than_the_first(both):
    """The film ends on the finished thing — every beat landed and held — and that
    final frame is the whole film for a reduced-motion viewer."""
    for shape, out in both.items():
        first = out["files"][f"custody-{shape}-first.jpg"].stat().st_size
        last = out["files"][f"custody-{shape}-last.jpg"].stat().st_size
        assert last > first, f"{shape}: last {last}B is not richer than first {first}B"


def test_the_renderer_accepts_the_composition_under_strict(both):
    """`--strict` makes the renderer's lint fatal, so reaching a file at all is the
    proof that no rule it enforces was broken — the layout-tween refusal first
    among them."""
    assert both["wide"]["files"]["custody-wide.mp4"].stat().st_size > 10_000


def test_a_second_render_is_the_same_film(tmp_path):
    """Deterministic by construction: no clock, no unseeded randomness, no network.
    Two renders of one composition are the same bytes."""
    html = film.compose(FILM, "wide")
    a = film.render(html, tmp_path / "a", "custody-wide")["files"]["custody-wide.mp4"]
    b = film.render(html, tmp_path / "b", "custody-wide")["files"]["custody-wide.mp4"]
    assert hashlib.sha256(a.read_bytes()).hexdigest() == \
        hashlib.sha256(b.read_bytes()).hexdigest()


def test_a_film_renders_without_the_design_checkout(tmp_path, monkeypatch):
    """Zen is the only face a Hanzo surface draws, and an image that shipped only
    Studio has no copy of it. The composition falls through its own font stack
    rather than rendering nothing."""
    monkeypatch.setattr(film.design, "faces", dict)
    out = film.render(film.compose(FILM, "wide"), tmp_path / "bare", "custody-wide")
    assert not (tmp_path / "bare" / "assets" / "zen.woff2").exists()
    assert out["measured"]["width"] == 1920
    assert out["files"]["custody-wide-first.jpg"].stat().st_size > 6_000


def test_a_composition_the_renderer_refuses_is_a_refusal_here(tmp_path):
    """Not a silent mp4 of the wrong thing: `--strict` turns a lint error into a
    non-zero exit, and the driver carries the renderer's own words out."""
    with pytest.raises(film.FilmError) as e:
        film.render("<!doctype html><html><body>no root</body></html>",
                    tmp_path / "broken", "custody-wide")
    assert "npx" in str(e.value)
