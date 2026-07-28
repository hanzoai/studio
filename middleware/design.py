"""
The Hanzo design system, read and edited in place.

Studio is the authoring surface for the design system it ships — the prompt it
hands its designer (design/prompts/studio-designer.md) says the agent "operates
on the `hanzoai/design` repository ... using its file tools". This is the file
seam under that: three kinds of artifact, each read from where it is authored
and written back to the same line.

  brand  — brand/brand.json's identity object, flattened to dotted names
           (``theme.dark.accent1``) so every value has exactly one address. Six
           of those fields and four theme values are mirrored in the package's
           typed registry; an edit here touches only brand.json, and the
           package's own consistency test is what catches the drift.
  tokens — design/tokens/*.css, the authored source of truth. A token is one
           ``--name: value;`` declaration and only the first wins — the rule
           design/scripts/gen-tokens.mjs applies to build the package's
           TypeScript view, so rewriting the CSS is the whole edit.
  marks  — logo/dist/*.svg, rendered from MARK_PATHS by the logo package's own
           build. Read-only: nothing in that chain is an editable file.

The three repos are studio's siblings in the checkout. Where they are absent —
an image that shipped only studio — every kind reads empty, and since an edit
may only name a value that already exists, every write is refused.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from aiohttp import web

_ROOT = Path(__file__).resolve().parents[2]     # the checkout studio sits in
_BRAND = _ROOT / "brand" / "brand.json"
_TOKENS = _ROOT / "design" / "tokens"
_MARKS = _ROOT / "logo" / "dist"

_COMMENT = re.compile(r"(/\*.*?\*/)", re.S)     # capturing: a split keeps the comments
_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")


def read() -> dict:
    """Every design-system value, by name."""
    return {"brand": _brand(), "tokens": _tokens(), "marks": _marks()}


def write(edit: dict) -> None:
    """Put each named value back in the file it was read from — a ``--name`` is a
    token, anything else a dotted path into the identity. Names must already exist:
    the design system is edited here, never invented, because a new token belongs
    in the package beside the comment that justifies it."""
    brand = {n: v for n, v in edit.items() if not n.startswith("--")}
    if brand:
        doc = json.loads(_BRAND.read_text())
        for name, value in brand.items():
            *path, leaf = name.split(".")
            node = doc["brand"]
            for k in path:
                node = node[k]
            node[leaf] = value
        _save(_BRAND, json.dumps(doc, indent=2) + "\n")

    pending = {n: v for n, v in edit.items() if n.startswith("--")}
    for f in sorted(_TOKENS.glob("*.css")):
        if not pending:
            break
        text = f.read_text()
        parts = _COMMENT.split(text)
        for i in range(0, len(parts), 2):
            for name, value in list(pending.items()):
                parts[i], hit = _decl(name).subn(lambda m: m.group(1) + value, parts[i], count=1)
                if hit:
                    del pending[name]
        out = "".join(parts)
        if out != text:
            _save(f, out)


def _brand() -> dict:
    try:
        doc = json.loads(_BRAND.read_text())
    except Exception:
        return {}
    out: dict = {}

    def walk(node: dict, prefix: str) -> None:
        for k, v in node.items():
            if isinstance(v, dict):
                walk(v, prefix + k + ".")
            else:
                out[prefix + k] = v

    walk(doc.get("brand") or {}, "")
    return out


def _tokens() -> dict:
    """Read the same even, comment-free chunks the writer rewrites — a declaration
    inside a comment is prose, and one straddling a comment is neither read nor
    written, so nothing is ever reported that cannot be edited."""
    out: dict = {}
    for f in sorted(_TOKENS.glob("*.css")):
        for part in _COMMENT.split(f.read_text())[::2]:
            for name, value in _DECL.findall(part):
                out.setdefault(name, value.strip())
    return out


def _marks() -> dict:
    return {f.stem: f.read_text() for f in sorted(_MARKS.glob("*.svg"))}


def _decl(name: str) -> re.Pattern:
    """A token's declaration. Everything through the colon is kept, so an edit
    leaves the file's authored indentation and spacing exactly as it was."""
    return re.compile("(" + re.escape(name) + r"\s*:[ \t]*)[^;\n]+")


# One value, and only the one declaration it belongs to. `[^;\n]+` bounds what a
# write REPLACES but not what it inserts, so a value carrying `;` appends a second
# declaration and one carrying a brace re-cuts the file. Either way the reader
# still reports the value that was sent — first-wins, like gen-tokens.mjs — while
# the cascade resolves the smuggled one, so what you read stops being what renders.
_UNSAFE = re.compile(r"[;{}\r\n]|/\*")


def _save(p: Path, text: str) -> None:
    """Atomic tmp+replace, the one write pattern for a file Studio owns."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)


def add_design_routes(routes: web.RouteTableDef, server) -> None:
    """Register /v1/design."""

    @routes.get("/v1/design")
    async def get_design(request):
        return web.json_response(read())

    @routes.patch("/v1/design")
    async def edit_design(request):
        """``{name: value}``. A mark, or a name the design system does not already
        carry, is refused — so a rendered artifact can never be written through."""
        try:
            edit = await request.json()
        except Exception:
            edit = None
        if not isinstance(edit, dict):
            return web.json_response({"error": "expected a JSON object of name: value"},
                                     status=400)
        current = read()
        names = current["brand"].keys() | current["tokens"].keys()
        unknown = sorted(n for n in edit if n not in names)
        if unknown:
            return web.json_response({"error": "unknown names", "names": unknown}, status=400)
        unsafe = sorted(n for n, v in edit.items() if _UNSAFE.search(str(v)))
        if unsafe:
            return web.json_response(
                {"error": "a value may not close its declaration or open a block",
                 "names": unsafe}, status=400)
        write(edit)
        return web.json_response(read())
