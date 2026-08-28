"""
Writing a hero film, and turning a composition into video.

A composition IS the artifact and Studio stores it as a `frame` document —
`documents.py` keeps it, `doc_render.composition()` reads what it declares. This
module is the two things that sit either side of that store, and it owns no
store, no surface and no second authoring model of its own.

WRITING ONE. `@hanzo/frame` is the player every Hanzo surface embeds —
`<Frame src="/hero" alt="…" />`, two props, no options — and `src` is the shared
PREFIX of six files whose names are the contract: a `-tall` and a `-wide` master,
and for each a `-first` still (frame 0, the poster) and a `-last` still (the final
frame, which is the whole film for a reduced-motion viewer). A hero film is one
message at two shapes, so it is a GENERATOR — a template, in the sense
`documents.py` already means — rather than two HTML files somebody keeps in step
by hand, which drift on the first copy edit.

The generator being the only writer is what turns rules that were learned
expensively into rules that cannot be broken. Every one of them renders
SUCCESSFULLY when it is wrong, which is exactly why they are generated instead of
remembered:

  * ``data-width`` / ``data-height`` / ``data-fps`` / ``data-duration`` are
    written from the shape and the beats. Without them a master renders 1080x1920
    portrait and nothing reports it.
  * The only animated properties are ``opacity`` and ``translateY``. The renderer
    REFUSES layout and text-reflow tweens — they snap to integer device pixels and
    stutter under seek-by-frame capture — and we render with ``--strict`` so that
    refusal is fatal rather than advisory.
  * The lead is drawn at full opacity from t=0, so frame 0 is never a black
    rectangle.
  * The last beat holds for seconds, so the film ends on the finished thing.
  * Nothing is dimmer than ``#777``, the measured floor that passes AA on true
    black, and nothing sits outside the middle of the tall master, which a 390px
    phone crops to.

Motion is CSS rather than a library. Capture polls for a GSAP timeline at
``window.__timelines[id]`` and, finding none, waits out the full player-ready
timeout — measured 51.5s against 5.8s for the same four seconds of film.
``data-no-timeline`` is the renderer's own opt-out for a CSS-driven composition,
and taking it means a composition is one self-contained file with no vendored
third-party script.

RENDERING ONE. The published `@hanzo/frames` CLI in headless Chrome, then ffmpeg
for the two stills — a subprocess, for any composition, whoever wrote it. Where
that toolchain is absent, and Studio's own image carries neither Node nor Chrome,
a render is REFUSED with the name of what is missing. It never half-succeeds and
never reports a film it did not make.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from middleware import design

# The renderer, pinned. One version reaches every film, and a bump is one edit
# here — the same pin the site's Makefiles carry, for the same reason.
RENDERER = "@hanzo/frames@0.7.83"

# Software rasterization and one worker: host GPU auto-probing fails capture
# calibration on this class of hardware and the render dies at frame 0.
_FLAGS = ["--quality", "high", "--no-browser-gpu", "--workers", "1", "--strict"]

_TIMEOUT = 900          # a 20s film at 30fps is ~60s; this is the ceiling
BEATS = 8               # a film nobody rewinds says at most this many things

# A stem names files, so it is held to what a filename may be.
STEM = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# The two shapes, and the numbers are measured rather than chosen. A phone is
# 0.46 wide-to-tall and a laptop 1.6, so one master cannot be both. `col` is the
# column the words live in: 840 of the tall master's 1080 keeps every word inside
# the middle 862px a 390x844 phone leaves after `object-fit: cover`.
SHAPES = {
    "tall": {"w": 1080, "h": 1920, "base": 22, "col": 840},
    "wide": {"w": 1920, "h": 1080, "base": 18, "col": 1240},
}

FPS = 30
_OPEN = 0.9      # the lead has the screen to itself before the first beat
_STEP = 0.7      # one beat to the next
_RISE = 0.6      # how long a beat takes to arrive
_HOLD = 3.0      # the finished film, held — this is the reduced-motion still


class FilmError(Exception):
    """A refusal with the real reason in it."""


def seconds(beats: list) -> float:
    """How long the film is, DERIVED. A duration typed beside a list of beats is a
    second copy of the list's length, and it is the copy that goes stale."""
    return round(_OPEN + max(len(beats) - 1, 0) * _STEP + _RISE + _HOLD, 1)


def slug(text: str) -> str:
    """A filename prefix from whatever the author typed."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())).strip("-")


def names(src: str) -> list[str]:
    """The six files `@hanzo/frame` resolves from one prefix. The names ARE the
    contract — change one and the hero goes blank — so they are stated once."""
    return [f"{src}-{shape}{suffix}"
            for shape in ("tall", "wide")
            for suffix in (".mp4", "-first.jpg", "-last.jpg")]


def call_site(src: str, alt: str) -> str:
    """The whole embed. Two props, because the component takes two."""
    return f'<Frame src="/{src}" alt="{str(alt).replace(chr(34), "&quot;")}" />'


# ── Writing one ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def compose(film: dict, shape: str) -> str:
    """One film, one shape, one HTML document. Pure: no clock, no filesystem, no
    network — the renderer bans all three, since a frame must be the same frame
    every time it is captured."""
    f = SHAPES[shape]
    b, col = f["base"], f["col"]
    got = list(film.get("beats") or [])[:BEATS]
    dur = seconds(got)
    beats = "".join(
        f'<div class="beat" style="animation-delay:{round(_OPEN + i * _STEP, 2)}s">'
        f'<span class="dot"></span><span class="head">{_esc(x.get("head"))}</span>'
        f'<span class="note">{_esc(x.get("note"))}</span></div>'
        for i, x in enumerate(got))
    foot = (f'<div class="foot" style="animation-delay:'
            f'{round(_OPEN + max(len(got) - 1, 0) * _STEP, 2)}s">{_esc(film["foot"])}</div>'
            ) if film.get("foot") else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={f['w']}, height={f['h']}" />
<title>{_esc(film.get('title') or 'Film')}</title>
<style>
@font-face {{ font-family: "Zen"; src: url("./assets/zen.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }}
@font-face {{ font-family: "Zen Mono"; src: url("./assets/zen-mono.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {f['w']}px; height: {f['h']}px; overflow: hidden; background: #000; }}
body {{ font-family: "Zen", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
       color: #ededed; -webkit-font-smoothing: antialiased; }}
#root {{ position: relative; width: {f['w']}px; height: {f['h']}px; overflow: hidden; background: #000; }}
.clip {{ position: absolute; inset: 0; }}
.glow {{ position: absolute; left: 50%; top: {round(f['h'] * 0.16)}px; width: {round(f['w'] * 1.05)}px;
        height: {round(f['h'] * 0.56)}px; margin-left: -{round(f['w'] * 0.525)}px; border-radius: 9999px;
        background: rgba(255,255,255,0.05); filter: blur(200px); }}
.stage {{ position: absolute; left: 50%; top: 50%; width: {col}px; margin-left: -{col // 2}px;
         transform: translateY(-50%); display: flex; flex-direction: column; gap: {round(b * 0.55)}px; }}
/* The lead carries no entrance. Frame 0 is the poster every motion viewer sees
   before the player takes over, and a poster of an empty black frame is the one
   thing this generator will not produce. */
.lead {{ font-size: {round(b * 2.1)}px; line-height: 1.12; font-weight: 600; color: #fff;
        letter-spacing: -0.02em; margin-bottom: {round(b * 1.4)}px; }}
.beat {{ display: flex; align-items: center; gap: {round(b * 0.85)}px; padding: {round(b * 0.9)}px {round(b * 1.1)}px;
        border: 1px solid #1f1f1f; border-radius: {round(b * 0.5)}px; background: #0b0b0b;
        opacity: 0; animation: rise {_RISE}s cubic-bezier(.16,1,.3,1) both; }}
.dot {{ width: {round(b * 0.42)}px; height: {round(b * 0.42)}px; border-radius: 999px; background: #fff; flex: none; }}
.head {{ font-size: {round(b * 1.05)}px; font-weight: 500; color: #fff; width: {round(col * 0.30)}px; flex: none; }}
.note {{ font-size: {round(b * 0.95)}px; color: #8e8e8e; }}
.foot {{ margin-top: {round(b * 1.6)}px; font-family: "Zen Mono", ui-monospace, monospace;
        font-size: {round(b * 0.86)}px; color: #777; opacity: 0;
        animation: rise {_RISE}s cubic-bezier(.16,1,.3,1) both; }}
/* opacity and translateY, and nothing else, ever. A layout or text-reflow
   property snaps to integer device pixels and stutters under seek-by-frame
   capture, which is why the renderer refuses to tween one. */
@keyframes rise {{ from {{ opacity: 0; transform: translateY({round(b * 1.2)}px); }}
                  to {{ opacity: 1; transform: translateY(0); }} }}
</style>
</head>
<body>
<!-- data-no-timeline: this composition is driven by CSS animation and registers
     no window.__timelines entry, so without the opt-out capture waits out the
     full player-ready timeout on every single render. -->
<div id="root" data-composition-id="main" data-no-timeline data-start="0"
     data-duration="{dur}" data-width="{f['w']}" data-height="{f['h']}" data-fps="{FPS}">
  <section id="scene" class="clip" data-start="0" data-duration="{dur}" data-track-index="1">
    <div class="glow"></div>
    <div class="stage">
      <div class="lead">{_esc(film.get('lead') or '')}</div>
      {beats}
      {foot}
    </div>
  </section>
</div>
</body>
</html>
"""


# ── Rendering one ─────────────────────────────────────────────────────────────

def tools() -> dict:
    """What a render needs, and where it is. An empty value is the honest answer
    and the reason a refusal can name what is missing."""
    return {t: shutil.which(t) or "" for t in ("npx", "node", "ffmpeg", "ffprobe")}


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FilmError(f"{cmd[0]} did not finish within {_TIMEOUT}s")
    except OSError as e:
        raise FilmError(f"{cmd[0]}: {e}")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-6:]
        raise FilmError(f"{cmd[0]} failed: " + " / ".join(tail))
    return p.stdout


def measure(mp4: Path) -> dict:
    """The master's own account of itself. A film that renders at the wrong size
    renders successfully, so the size is read back rather than assumed."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height", "-show_entries", "format=duration",
                "-of", "json", str(mp4)])
    d = json.loads(out)
    s = (d.get("streams") or [{}])[0]
    return {"width": s.get("width"), "height": s.get("height"),
            "seconds": round(float(d.get("format", {}).get("duration", 0)), 2)}


def render(html: str, work: Path, stem: str) -> dict:
    """One composition to its master and the two stills that go with it. Named from
    the stem, which is the half of a film's six-file contract this shape owns."""
    missing = [t for t, p in tools().items() if not p]
    if missing:
        raise FilmError("this machine cannot render a composition: no " + ", ".join(missing))
    if not STEM.match(stem or ""):
        raise FilmError("a stem names files, so it is lowercase words joined by hyphens")
    work.mkdir(parents=True, exist_ok=True)
    (work / "assets").mkdir(exist_ok=True)
    (work / "index.html").write_text(html)
    for family, path in design.faces().items():
        shutil.copyfile(path, work / "assets" /
                        ("zen-mono.woff2" if "Mono" in family else "zen.woff2"))

    mp4, first, last = f"{stem}.mp4", f"{stem}-first.jpg", f"{stem}-last.jpg"
    # cwd is the project: the CLI resolves --output against the process directory,
    # not against the project it was handed.
    _run(["npx", "--yes", RENDERER, "render", *_FLAGS, "--output", mp4], cwd=work)
    if not (work / mp4).is_file():
        raise FilmError(f"the renderer reported success and wrote no {mp4}")
    # Frame 0 is the poster; the last frame is the film for anyone who asked not to
    # see it. -sseof seeks from the end, the only way to name the last frame without
    # knowing the frame count.
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4,
          "-vf", "select=eq(n\\,0)", "-vframes", "1", "-q:v", "3", first], cwd=work)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", mp4,
          "-update", "1", "-q:v", "3", last], cwd=work)
    return {"files": {n: work / n for n in (mp4, first, last)},
            "measured": measure(work / mp4)}
