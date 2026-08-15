"""
A film, authored here and rendered here.

`@hanzo/frame` is the player every Hanzo surface embeds — `<Frame src="/hero"
alt="…" />`, two props, no options — and `src` is the shared PREFIX of six files
whose names are the contract: a `-tall.mp4` and a `-wide.mp4` master, and for
each a `-first` still (frame 0, the poster) and a `-last` still (the final
frame, which is the whole film for a reduced-motion viewer). Until now the six
were produced by copying a directory of hand-written HTML into a website's
repository, editing it, and running `make`. That is a developer's workflow in
one repo; this is the same six files as an operation on a document.

A FILM IS ITS BEATS, and the composition is a derivation — the same shape
`documents.py` takes, for the same reason. An author edits the words; the HTML
that goes to the renderer is written from them, twice, once per shape. Nobody
hand-keeps two files that must say the same thing, because the first copy edit
drifts them.

What the generator being the only writer buys is that the rules learned
expensively stop being prose someone has to remember:

  * ``data-width`` / ``data-height`` / ``data-fps`` / ``data-duration`` are
    written from the shape and from the beats. A master that renders 1080x1920
    when it meant to be 1920x1080 is the failure those attributes exist to
    prevent, and it is now unreachable — there is no path through here that
    omits them.
  * The only animated properties are ``opacity`` and ``translateY``. The
    renderer REFUSES layout and text-reflow tweens (they snap to integer device
    pixels and stutter under frame-seek capture) and we render with ``--strict``
    so that refusal is fatal rather than advisory.
  * The lead is drawn at full opacity from t=0, so frame 0 — the poster a motion
    viewer stares at until the player takes over — is never a black rectangle.
  * The last beat holds for seconds at the end, so the final frame is the
    finished thing, which is what a reduced-motion viewer is served instead of
    the film.
  * Nothing is dimmer than ``#777``: the measured floor that passes AA on true
    black. Nothing sits outside the safe middle of the tall master, which a
    390px phone crops to.

Motion is CSS, not a library. The renderer's own capture waits for a GSAP
timeline at ``window.__timelines[id]`` and stalls the full 45s player timeout
when none arrives; ``data-no-timeline`` on the root is its documented opt-out
for compositions driven purely by CSS animation, and taking it costs a film
nothing and saves 45 seconds a render. It also means a composition is one
self-contained file with no vendored third-party script.

The render is a subprocess: the published `@hanzo/frames` CLI in headless
Chrome, then ffmpeg for the four stills. Where that toolchain is absent —
studio's own image carries no Node and no Chrome today — every render is
REFUSED with the name of what is missing. It never half-succeeds and it never
reports a film it did not make.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web

from middleware import design

# The renderer, pinned. One version reaches every film, and a bump is one edit
# here — the same pin the site's Makefiles carry, for the same reason.
RENDERER = "@hanzo/frames@0.7.83"

# Software rasterization and one worker: host GPU auto-probing fails capture
# calibration on this class of hardware and the render dies at frame 0.
_FLAGS = ["--quality", "high", "--no-browser-gpu", "--workers", "1", "--strict"]

_RENDER_TIMEOUT = 900          # a 20s film at 30fps is ~60s; this is the ceiling
_ID = re.compile(r"^[0-9a-f]{32}\Z")   # \Z, so a trailing newline cannot pass
_SRC = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_BEATS = 8                     # a film nobody rewinds says at most this many things
_MAX = 4000                    # bytes of words in one film

# The two shapes, and the numbers are measured rather than chosen. A phone is
# 0.46 wide-to-tall and a laptop 1.6, so one master cannot be both. `col` is the
# column the words live in: 840 of the tall master's 1080 keeps every word
# inside the middle 862px a 390x844 phone leaves after `object-fit: cover`.
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
    """How long the film is, DERIVED. A duration typed beside a list of beats is
    a second copy of the list's length, and it is the copy that goes stale."""
    return round(_OPEN + max(len(beats) - 1, 0) * _STEP + _RISE + _HOLD, 1)


def names(src: str) -> list[str]:
    """The six files `@hanzo/frame` resolves from one prefix. The names ARE the
    contract — change one and the hero goes blank — so they are stated once."""
    out = []
    for shape in ("tall", "wide"):
        out.append(f"{src}-{shape}.mp4")
        out += [f"{src}-{shape}-first.jpg", f"{src}-{shape}-last.jpg"]
    return out


def call_site(film: dict) -> str:
    """The whole embed. Two props, because the component takes two."""
    alt = film["alt"].replace('"', "&quot;")
    return f'<Frame src="/{film["src"]}" alt="{alt}" />'


# ── The composition ───────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def compose(film: dict, shape: str) -> str:
    """One film, one shape, one HTML document. Pure: no clock, no filesystem, no
    network — the renderer bans all three anyway, since a frame must be the same
    frame every time it is captured."""
    f = SHAPES[shape]
    b, col, dur = f["base"], f["col"], seconds(film["beats"])
    beats = "".join(
        f'<div class="beat" style="animation-delay:{round(_OPEN + i * _STEP, 2)}s">'
        f'<span class="dot"></span><span class="head">{_esc(x["head"])}</span>'
        f'<span class="note">{_esc(x["note"])}</span></div>'
        for i, x in enumerate(film["beats"]))
    foot = (f'<div class="foot" style="animation-delay:{round(_OPEN + max(len(film["beats"]) - 1, 0) * _STEP, 2)}s">'
            f'{_esc(film["foot"])}</div>') if film.get("foot") else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={f['w']}, height={f['h']}" />
<title>{_esc(film['title'])}</title>
<style>
@font-face {{ font-family: "Geist"; src: url("./assets/geist.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }}
@font-face {{ font-family: "Geist Mono"; src: url("./assets/geist-mono.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {f['w']}px; height: {f['h']}px; overflow: hidden; background: #000; }}
body {{ font-family: "Geist", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
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
.foot {{ margin-top: {round(b * 1.6)}px; font-family: "Geist Mono", ui-monospace, monospace;
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
     full 45s player-ready timeout on every single render. -->
<div id="root" data-composition-id="main" data-no-timeline data-start="0"
     data-duration="{dur}" data-width="{f['w']}" data-height="{f['h']}" data-fps="{FPS}">
  <section id="scene" class="clip" data-start="0" data-duration="{dur}" data-track-index="1">
    <div class="glow"></div>
    <div class="stage">
      <div class="lead">{_esc(film['lead'])}</div>
      {beats}
      {foot}
    </div>
  </section>
</div>
</body>
</html>
"""


# ── Reading a film ────────────────────────────────────────────────────────────

def read(body) -> dict:
    """A film, or the reason this is not one. Every field is words except `src`,
    which is a filename prefix and is held to one."""
    if not isinstance(body, dict):
        raise FilmError("expected a film object")
    out = {}
    for key in ("title", "alt", "lead"):
        v = body.get(key)
        if not isinstance(v, str) or not v.strip():
            raise FilmError(f"{key} is the film's own words and cannot be empty")
        out[key] = v.strip()
    src = (body.get("src") or "").strip()
    if not _SRC.match(src):
        raise FilmError("src names six files, so it is lowercase words joined by hyphens")
    out["src"] = src
    beats = body.get("beats")
    if not isinstance(beats, list) or not beats:
        raise FilmError("a film says something, so it has at least one beat")
    if len(beats) > _BEATS:
        raise FilmError(f"a film nobody rewinds holds at most {_BEATS} beats")
    out["beats"] = []
    for i, x in enumerate(beats):
        if not isinstance(x, dict):
            raise FilmError(f"beat {i + 1} is an object of head and note")
        head, note = str(x.get("head", "")).strip(), str(x.get("note", "")).strip()
        if not head:
            raise FilmError(f"beat {i + 1} has no head, and the head is what it says")
        out["beats"].append({"head": head, "note": note})
    out["foot"] = str(body.get("foot") or "").strip()
    if len(json.dumps(out)) > _MAX:
        raise FilmError("a film is a fold, not a page — this is too many words")
    return out


# ── The render ────────────────────────────────────────────────────────────────

def tools() -> dict:
    """What a render needs, and where it is. An empty value is the honest answer
    and the reason a refusal can name what is missing."""
    return {t: shutil.which(t) or "" for t in ("npx", "node", "ffmpeg", "ffprobe")}


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=_RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FilmError(f"{cmd[0]} did not finish within {_RENDER_TIMEOUT}s")
    except OSError as e:
        raise FilmError(f"{cmd[0]}: {e}")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-6:]
        raise FilmError(f"{cmd[0]} failed: " + " / ".join(tail))
    return p.stdout


def measure(mp4: Path) -> dict:
    """The master's own account of itself. A film that renders the wrong size
    renders successfully, so the size is read back rather than assumed."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height", "-show_entries", "format=duration",
                "-of", "json", str(mp4)])
    d = json.loads(out)
    s = (d.get("streams") or [{}])[0]
    return {"width": s.get("width"), "height": s.get("height"),
            "seconds": round(float(d.get("format", {}).get("duration", 0)), 2)}


def render(film: dict, work: Path) -> dict:
    """Compose both shapes, render both masters, cut the four stills. Returns the
    six files by name, each measured. The whole thing or the reason why not."""
    missing = [t for t, p in tools().items() if not p]
    if missing:
        raise FilmError("this machine cannot render a film: no " + ", ".join(missing))
    work.mkdir(parents=True, exist_ok=True)
    made, size = {}, {}
    for shape in SHAPES:
        d = work / shape
        (d / "assets").mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(compose(film, shape))
        for family, path in design.faces().items():
            shutil.copyfile(path, d / "assets" /
                            ("geist-mono.woff2" if "Mono" in family else "geist.woff2"))
        mp4 = f"{film['src']}-{shape}.mp4"
        # cwd is the project: the CLI resolves --output against the process
        # directory, not against the project it was handed.
        _run(["npx", "--yes", RENDERER, "render", *_FLAGS, "--output", mp4], cwd=d)
        if not (d / mp4).is_file():
            raise FilmError(f"the renderer reported success and wrote no {mp4}")
        # Frame 0 is the poster; the last frame is the film for anyone who asked
        # not to see it. -sseof seeks from the end, the only way to name the last
        # frame without knowing the frame count.
        first, last = f"{film['src']}-{shape}-first.jpg", f"{film['src']}-{shape}-last.jpg"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4,
              "-vf", "select=eq(n\\,0)", "-vframes", "1", "-q:v", "3", first], cwd=d)
        _run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", mp4,
              "-update", "1", "-q:v", "3", last], cwd=d)
        for n in (mp4, first, last):
            made[n] = d / n
        size[shape] = measure(d / mp4)
    return {"files": made, "measured": size}


# ── The store — per-org, atomic, the same tenancy every other module has ───────

def _root(org: str) -> Path:
    import folder_paths
    out = Path(folder_paths.get_org_output_directory(org))
    base = out.parent if out.parent.name == org else out
    d = base / "films"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _index(org: str) -> dict:
    f = _root(org) / "index.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"films": []}


def _load(org: str, fid: str) -> dict | None:
    if not _ID.match(fid or ""):
        return None
    try:
        return json.loads((_root(org) / fid / "film.json").read_text())
    except Exception:
        return None


def _save(org: str, doc: dict) -> None:
    d = _root(org) / doc["id"]
    d.mkdir(parents=True, exist_ok=True)
    _write(d / "film.json", doc)
    idx = _index(org)
    row = {"id": doc["id"], "title": doc["film"]["title"], "src": doc["film"]["src"],
           "seconds": seconds(doc["film"]["beats"]), "updatedAt": doc["updatedAt"],
           "rendered": doc.get("rendered") or []}
    films = [r for r in idx.get("films", []) if r.get("id") != doc["id"]]
    _write(_root(org) / "index.json", {"films": [row] + films})


def _public(doc: dict) -> dict:
    return dict(doc, seconds=seconds(doc["film"]["beats"]),
                names=names(doc["film"]["src"]), frame=call_site(doc["film"]))


# ── REST ──────────────────────────────────────────────────────────────────────

def add_film_routes(routes: web.RouteTableDef, server, *, org_of, owner_of=None,
                    land=None) -> None:
    """Register /v1/films. `land(org, rel)` — injected — puts a rendered file into
    the org's library at the one ingest point every other render goes through, so
    a film's six files are gallery assets like anything else Studio makes."""

    @routes.get("/v1/films")
    async def list_films(request: web.Request):
        return web.json_response({"films": _index(org_of(request)).get("films", []),
                                  "shapes": SHAPES, "renderer": RENDERER,
                                  "tools": tools()})

    @routes.post("/v1/films")
    async def create(request: web.Request):
        try:
            film = read(await request.json())
        except FilmError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            return web.json_response({"error": "expected a film object"}, status=400)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        doc = {"id": uuid.uuid4().hex, "film": film, "rendered": [],
               "owner": owner_of(request) if owner_of else "",
               "createdAt": now, "updatedAt": now}
        _save(org_of(request), doc)
        return web.json_response(_public(doc), status=201)

    @routes.get("/v1/films/{id}")
    async def get_film(request: web.Request):
        doc = _load(org_of(request), request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(_public(doc))

    @routes.patch("/v1/films/{id}")
    async def edit(request: web.Request):
        org = org_of(request)
        doc = _load(org, request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            doc["film"] = read({**doc["film"], **(await request.json())})
        except FilmError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            return web.json_response({"error": "expected a film object"}, status=400)
        doc["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save(org, doc)
        return web.json_response(_public(doc))

    @routes.delete("/v1/films/{id}")
    async def remove(request: web.Request):
        org = org_of(request)
        doc = _load(org, request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        shutil.rmtree(_root(org) / doc["id"], ignore_errors=True)
        _write(_root(org) / "index.json",
               {"films": [r for r in _index(org).get("films", []) if r.get("id") != doc["id"]]})
        return web.json_response({"ok": True})

    @routes.get("/v1/films/{id}/composition")
    async def composition(request: web.Request):
        """The HTML that goes to the renderer, for the shape asked for. This is
        what makes the derivation inspectable rather than a black box."""
        doc = _load(org_of(request), request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        shape = request.query.get("shape", "wide")
        if shape not in SHAPES:
            return web.json_response({"error": f"shape is one of {', '.join(SHAPES)}"},
                                     status=400)
        return web.Response(text=compose(doc["film"], shape), content_type="text/html")

    @routes.post("/v1/films/{id}/render")
    async def make(request: web.Request):
        """Render both masters and cut the four stills. Synchronous, because a
        caller that is told a film is ready is told it by the thing that made it."""
        org = org_of(request)
        doc = _load(org, request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        work = _root(org) / doc["id"] / "render"
        shutil.rmtree(work, ignore_errors=True)
        try:
            out = render(doc["film"], work)
        except FilmError as e:
            return web.json_response({"error": str(e), "tools": tools()}, status=503)
        landed = []
        for name, path in out["files"].items():
            rel = f"film/{name}"
            if land:
                land(org, rel, path)
            landed.append(rel)
        doc["rendered"] = landed
        doc["measured"] = out["measured"]
        doc["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save(org, doc)
        return web.json_response(_public(doc))
