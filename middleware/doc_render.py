"""Office-document renderers — the NON-GPU pipeline.

Studio's creative side is ComfyUI graphs → GPU. Office documents are the opposite:
structured content (a template's typed fields, filled by a person or by the AI) →
a pure-Python renderer → a real .pdf/.docx/.xlsx/.pptx/.md file. No graph, no model,
no GPU.

One and only one way, kept DRY by a neutral intermediate representation (IR):

  * a template maps its fields to ONE of three archetype IRs —
      - ``doc``   : a list of prose blocks (title/heading/paragraph/table/…)  → PDF · DOCX · MD
      - ``sheet`` : a list of sheets (columns + rows + totals)                 → XLSX · PDF · DOCX · MD
      - ``deck``  : a list of slides (title + bullets + notes)                 → PPTX · PDF · MD
  * each output format has EXACTLY ONE renderer that consumes the IR.

So adding a template = write one ``build(fields) -> (kind, ir)``; adding a format =
write one renderer. The renderers never know about templates, HTTP, storage, or org
scoping — they are pure ``(ir) -> bytes``.

Dependencies are imported LAZILY inside each renderer so this module imports with only
the standard library present (unit tests, light pods). PDF prefers WeasyPrint (full CSS
fidelity); when its native stack (pango/cairo) is absent it falls back to a self-contained
pure-Python PDF writer that still emits a valid, readable, monochrome document — the
``weasyprint``-absent fallback. DOCX/XLSX/PPTX are pure-Python (python-docx / openpyxl /
python-pptx) and always render for real.
"""
from __future__ import annotations

import html as _html
import io
import math
import zlib
from html.parser import HTMLParser
from typing import Any

# ── Public surface ────────────────────────────────────────────────────────────────

FORMATS = ("pdf", "docx", "xlsx", "pptx", "md", "html")

CONTENT_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}

# Which formats each archetype can produce. The catalog's per-template ``formats`` is
# always a subset of its archetype's capability — the render dispatch enforces both.
KIND_FORMATS = {
    "doc": ("pdf", "docx", "md"),
    "sheet": ("xlsx", "pdf", "docx", "md"),
    "deck": ("pptx", "pdf", "md"),
    "frame": ("html",),
}

# The one format each archetype draws with the standard library alone. Every write
# renders to it to prove the content is renderable before anything is stored, so it must
# never be the format that also asks whether some wheel is installed here. For prose that
# is Markdown; a composition already IS its HTML.
PROOF = {"doc": "md", "sheet": "md", "deck": "md", "frame": "html"}


# The grammar an author writes, published so a person or an agent can be ACCURATE
# instead of guessing. It lives beside the renderers because they are the only thing
# that decides what a block means; ``doc_render_test`` walks the doc renderer and fails
# if a tag here is one it does not draw, or if a tag it draws is missing here.
GRAMMAR = {
    "doc": {
        "of": "prose — a title, headings, paragraphs, tables, lists",
        "shape": {"title": "str", "blocks": "[block]", "meta": "{}"},
        "blocks": {
            "title": {"text": "str", "sub": "str"},
            "h": {"text": "str", "level": "1|2|3"},
            "p": {"text": "str — newlines break lines"},
            "kv": {"pairs": "[[key, value]]", "cols": "1|2"},
            "party": {"left": "{title, lines:[str]}", "right": "{title, lines:[str]}"},
            "table": {"headers": "[str]", "rows": "[[cell]]",
                      "align": "[''|'num'] per column", "foot": "[[cell]]"},
            "ul": {"items": "[str]"},
            "hr": {},
            "sp": {"h": "points"},
            "sign": {"label": "str"},
        },
    },
    "sheet": {
        "of": "tabular data — one or more named sheets",
        "shape": {"title": "str", "sheets": "[sheet]"},
        "sheet": {"name": "str",
                  "columns": "[{key, label, width, fmt}] — fmt money|int|pct|date|null",
                  "rows": "[{key: value}]", "totals": "[{key: value}]"},
    },
    "deck": {
        "of": "slides",
        "shape": {"title": "str", "slides": "[slide]"},
        "slide": {"layout": "title|bullets|section", "title": "str", "subtitle": "str",
                  "bullets": "[str]", "notes": "str"},
    },
    "frame": {
        "of": "a Frames composition — HTML that plays; a page for web and ads, and what "
              "`frames render` turns into video",
        "shape": {"title": "str", "html": "str — the whole document"},
        "root": {"data-composition-id": "str, usually 'main' — names the timeline",
                 "data-width": "px", "data-height": "px",
                 "data-duration": "seconds; may be left off when the timeline implies it"},
        "clip": {"class": "clip — on every visible timed element, a direct child of root",
                 "id": "str", "data-start": "seconds", "data-duration": "seconds",
                 "data-track-index": "int — clips on one track never overlap"},
        "motion": "one paused GSAP timeline per composition at window.__timelines[id], "
                  "built synchronously at load",
        "deterministic": "no Date.now, no unseeded Math.random, no fetch while rendering, "
                         "no endless repeat",
        "assets": "reference them by URL — a document is one file, so a composition that "
                  "needs a folder beside it belongs in a frames project",
        "authority": "hanzoai/frames — `frames lint` and `frames check` are the full "
                     "check, and they run where the renderer runs",
    },
}


def grammar() -> dict:
    """The grammar plus the formats each archetype renders. One value, two facts."""
    return {k: {**v, "formats": list(KIND_FORMATS[k])} for k, v in GRAMMAR.items()}


def content_type(fmt: str) -> str:
    return CONTENT_TYPE[fmt]


def render(kind: str, ir: dict, fmt: str) -> bytes:
    """Render an archetype IR to ``fmt``, returning the file bytes.

    Raises ``ValueError`` for an unsupported (kind, fmt) pair — the one place the
    dispatch table lives, so a caller can never silently get the wrong renderer.
    """
    if fmt not in KIND_FORMATS.get(kind, ()):  # pragma: no cover - guarded by callers
        raise ValueError(f"{kind!r} cannot render {fmt!r}")
    if kind == "doc":
        return _render_doc(ir, fmt)
    if kind == "sheet":
        return _render_sheet(ir, fmt)
    if kind == "deck":
        return _render_deck(ir, fmt)
    if kind == "frame":
        return _render_frame(ir)
    raise ValueError(f"unknown archetype {kind!r}")  # pragma: no cover


def weasyprint_available() -> bool:
    """True when the high-fidelity PDF path (WeasyPrint + native pango/cairo) is usable.
    Diagnostic only — the renderer falls back automatically."""
    return _weasy_pdf("<html><body>probe</body></html>") is not None


# ── IR block constructors — the shared vocabulary of the ``doc`` archetype ─────────
# Blocks are plain dicts (JSON-friendly, no classes to import). A template's build()
# assembles a list of them; four renderers consume the same list.

def title(text: str, sub: str | None = None) -> dict:
    return {"t": "title", "text": text or "", "sub": sub or ""}


def heading(text: str, level: int = 2) -> dict:
    return {"t": "h", "text": text or "", "level": max(1, min(3, int(level)))}


def para(text: str) -> dict:
    return {"t": "p", "text": text or ""}


def keyvals(pairs: list, cols: int = 1) -> dict:
    return {"t": "kv", "pairs": [[str(k), str(v)] for k, v in pairs if v not in (None, "")], "cols": cols}


def party(left: dict, right: dict | None = None) -> dict:
    """Two facing address/identity blocks (invoice from/to, letter sender/recipient)."""
    def _side(s):
        s = s or {}
        return {"title": str(s.get("title", "")), "lines": [str(x) for x in (s.get("lines") or []) if str(x).strip()]}
    return {"t": "party", "left": _side(left), "right": _side(right)}


def table(headers: list, rows: list, align: list | None = None, foot: list | None = None) -> dict:
    return {
        "t": "table",
        "headers": [str(h) for h in headers],
        "rows": [[("" if c is None else str(c)) for c in r] for r in rows],
        "align": align or [],
        "foot": [[("" if c is None else str(c)) for c in r] for r in (foot or [])],
    }


def bullets(items: list) -> dict:
    return {"t": "ul", "items": [str(i) for i in items if str(i).strip()]}


def rule() -> dict:
    return {"t": "hr"}


def spacer(h: int = 12) -> dict:
    return {"t": "sp", "h": int(h)}


def signature(label: str) -> dict:
    return {"t": "sign", "label": label or ""}


def doc_ir(title_text: str, blocks: list, *, meta: dict | None = None) -> dict:
    return {"title": title_text or "Document", "blocks": [b for b in blocks if b], "meta": meta or {}}


# ── doc archetype: PDF · DOCX · MD ────────────────────────────────────────────────

def _render_doc(ir: dict, fmt: str) -> bytes:
    if fmt == "pdf":
        html = _doc_html(ir)
        pdf = _weasy_pdf(html)
        return pdf if pdf is not None else _builtin_pdf(ir)
    if fmt == "docx":
        return _doc_docx(ir)
    if fmt == "md":
        return _doc_md(ir).encode("utf-8")
    raise ValueError(fmt)  # pragma: no cover


# Monochrome print stylesheet — Hanzo's design-system look on paper: near-black ink on
# white, a clean sans stack, thin hairline rules, generous margins. Deliberately light
# (this is the paper, previewed as paper even under a dark UI).
_PRINT_CSS = """
@page { size: Letter; margin: 22mm 20mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, "Segoe UI", sans-serif;
  color: #16171a; font-size: 10.5pt; line-height: 1.5; margin: 0; }
h1.doctitle { font-size: 22pt; letter-spacing: -.01em; margin: 0 0 2pt; font-weight: 700; }
.docsub { color: #6b6f76; font-size: 10.5pt; margin: 0 0 14pt; }
h2 { font-size: 13pt; font-weight: 700; margin: 20pt 0 6pt; letter-spacing:-.005em; }
h3 { font-size: 11pt; font-weight: 700; margin: 14pt 0 4pt; }
h4 { font-size: 10.5pt; font-weight: 700; margin: 12pt 0 3pt; text-transform: uppercase; letter-spacing: .06em; color:#4a4d53; }
p { margin: 0 0 8pt; }
.hr { border: 0; border-top: 1px solid #d9dbdf; margin: 14pt 0; }
.kv { width: 100%; border-collapse: collapse; margin: 0 0 8pt; }
.kv td { padding: 2pt 0; vertical-align: top; }
.kv td.k { color: #6b6f76; width: 34%; padding-right: 10pt; }
.kv.two { }
.parties { display: flex; gap: 26pt; margin: 6pt 0 16pt; }
.parties .side { flex: 1; }
.parties .side .ttl { text-transform: uppercase; letter-spacing: .06em; font-size: 8.5pt; color: #6b6f76; margin: 0 0 3pt; }
.parties .side .ln { margin: 0; }
table.grid { width: 100%; border-collapse: collapse; margin: 8pt 0 12pt; font-size: 10pt; }
table.grid th { text-align: left; font-weight: 700; border-bottom: 1.4px solid #16171a; padding: 6pt 8pt 6pt 0; }
table.grid td { border-bottom: 1px solid #e6e8eb; padding: 6pt 8pt 6pt 0; vertical-align: top; }
table.grid th.num, table.grid td.num { text-align: right; padding-right: 0; }
table.grid tfoot td { border-bottom: 0; border-top: 1.4px solid #16171a; font-weight: 700; padding-top: 7pt; }
ul { margin: 0 0 8pt; padding-left: 16pt; }
li { margin: 0 0 3pt; }
.sign { margin-top: 34pt; }
.sign .line { border-top: 1px solid #16171a; width: 58%; padding-top: 4pt; color: #6b6f76; font-size: 9pt; }
"""


def _doc_html(ir: dict) -> str:
    """The IR as one self-contained HTML document for WeasyPrint. Built with stdlib
    ``html.escape`` at every value boundary — no template engine, so there is no
    template-injection surface and no extra dependency; the structural HTML is ours,
    every field value is escaped."""
    body = _doc_html_body(ir)  # already-escaped, structural HTML
    doc_title = _html.escape(ir.get("title") or "Document")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>" + doc_title
        + "</title><style>" + _PRINT_CSS + "</style></head><body>" + body + "</body></html>"
    )


def _e(s: Any) -> str:
    return _html.escape("" if s is None else str(s))


def _doc_html_body(ir: dict) -> str:
    out: list[str] = []
    for b in ir.get("blocks", []):
        t = b.get("t")
        if t == "title":
            out.append(f"<h1 class='doctitle'>{_e(b['text'])}</h1>")
            if b.get("sub"):
                out.append(f"<div class='docsub'>{_e(b['sub'])}</div>")
        elif t == "h":
            tag = {1: "h2", 2: "h3", 3: "h4"}[b.get("level", 2)]
            out.append(f"<{tag}>{_e(b['text'])}</{tag}>")
        elif t == "p":
            out.append(f"<p>{_e(b['text']).replace(chr(10), '<br>')}</p>")
        elif t == "kv":
            cls = "kv two" if b.get("cols") == 2 else "kv"
            rows = "".join(f"<tr><td class='k'>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in b["pairs"])
            out.append(f"<table class='{cls}'>{rows}</table>")
        elif t == "party":
            def side(s):
                lns = "".join(f"<p class='ln'>{_e(x)}</p>" for x in s["lines"])
                ttl = f"<p class='ttl'>{_e(s['title'])}</p>" if s["title"] else ""
                return f"<div class='side'>{ttl}{lns}</div>"
            out.append(f"<div class='parties'>{side(b['left'])}{side(b['right'])}</div>")
        elif t == "table":
            out.append(_html_table(b))
        elif t == "ul":
            out.append("<ul>" + "".join(f"<li>{_e(i)}</li>" for i in b["items"]) + "</ul>")
        elif t == "hr":
            out.append("<hr class='hr'>")
        elif t == "sp":
            out.append(f"<div style='height:{int(b.get('h', 12))}pt'></div>")
        elif t == "sign":
            out.append(f"<div class='sign'><div class='line'>{_e(b['label'])}</div></div>")
    return "".join(out)


def _num_cols(b: dict) -> set:
    return {i for i, a in enumerate(b.get("align", [])) if a == "num"}


def _html_table(b: dict) -> str:
    nums = _num_cols(b)
    th = "".join(f"<th class='{'num' if i in nums else ''}'>{_e(h)}</th>" for i, h in enumerate(b["headers"]))
    body = "".join(
        "<tr>" + "".join(f"<td class='{'num' if i in nums else ''}'>{_e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in b["rows"]
    )
    foot = ""
    if b.get("foot"):
        foot = "<tfoot>" + "".join(
            "<tr>" + "".join(f"<td class='{'num' if i in nums else ''}'>{_e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
            for r in b["foot"]
        ) + "</tfoot>"
    return f"<table class='grid'><thead><tr>{th}</tr></thead><tbody>{body}</tbody>{foot}</table>"


_WEASY: Any = ...  # unset sentinel; set to the module or False on first probe


def _weasyprint():
    """The weasyprint module if importable with its native stack, else False. Probed
    once and cached — the import is expensive and, when pango/cairo are absent, prints
    its own diagnostics, so we do it exactly once and swallow that chatter."""
    global _WEASY
    if _WEASY is ...:
        import contextlib
        import os as _os
        try:
            with open(_os.devnull, "w") as dn, contextlib.redirect_stderr(dn), contextlib.redirect_stdout(dn):
                import weasyprint  # type: ignore
            _WEASY = weasyprint
        except Exception:
            _WEASY = False
    return _WEASY


def _weasy_pdf(html: str) -> bytes | None:
    """WeasyPrint render, or None when the library or its native stack is unavailable —
    the caller then uses the built-in fallback. Fully guarded so a pod without
    pango/cairo simply falls back."""
    wp = _weasyprint()
    if not wp:
        return None
    try:
        return wp.HTML(string=html).write_pdf()
    except Exception:  # pragma: no cover - native failure at render time
        return None


# ── DOCX (python-docx) ────────────────────────────────────────────────────────────

def _doc_docx(ir: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    INK = RGBColor(0x16, 0x17, 0x1A)
    DIM = RGBColor(0x6B, 0x6F, 0x76)
    d = Document()
    for s in d.styles:
        try:
            if s.font is not None and s.font.name is None:
                s.font.name = "Helvetica"
        except Exception:
            pass
    normal = d.styles["Normal"]
    normal.font.name = "Helvetica"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = INK

    def _runcolor(p, color=INK):
        for r in p.runs:
            r.font.color.rgb = color

    for b in ir.get("blocks", []):
        t = b.get("t")
        if t == "title":
            p = d.add_paragraph()
            run = p.add_run(b["text"])
            run.bold = True
            run.font.size = Pt(22)
            run.font.color.rgb = INK
            if b.get("sub"):
                sp = d.add_paragraph()
                sr = sp.add_run(b["sub"])
                sr.font.size = Pt(10.5)
                sr.font.color.rgb = DIM
        elif t == "h":
            p = d.add_paragraph()
            run = p.add_run(b["text"])
            run.bold = True
            run.font.size = Pt({1: 13, 2: 11, 3: 10.5}[b.get("level", 2)])
            run.font.color.rgb = INK if b.get("level", 2) < 3 else DIM
        elif t == "p":
            p = d.add_paragraph(b["text"])
            _runcolor(p)
        elif t == "kv":
            tbl = d.add_table(rows=0, cols=2)
            for k, v in b["pairs"]:
                cells = tbl.add_row().cells
                kr = cells[0].paragraphs[0].add_run(k)
                kr.font.color.rgb = DIM
                kr.font.size = Pt(10)
                vr = cells[1].paragraphs[0].add_run(v)
                vr.font.size = Pt(10)
                vr.font.color.rgb = INK
        elif t == "party":
            tbl = d.add_table(rows=1, cols=2)
            for cell, side in ((tbl.rows[0].cells[0], b["left"]), (tbl.rows[0].cells[1], b["right"])):
                if side["title"]:
                    tr = cell.paragraphs[0].add_run(side["title"].upper())
                    tr.bold = True
                    tr.font.size = Pt(8.5)
                    tr.font.color.rgb = DIM
                for ln in side["lines"]:
                    lp = cell.add_paragraph()
                    lr = lp.add_run(ln)
                    lr.font.size = Pt(10)
                    lr.font.color.rgb = INK
        elif t == "table":
            _docx_table(d, b, INK, DIM, Pt)
        elif t == "ul":
            for it in b["items"]:
                p = d.add_paragraph(it, style="List Bullet")
                _runcolor(p)
        elif t == "hr":
            d.add_paragraph("_" * 48).runs[0].font.color.rgb = DIM
        elif t == "sp":
            d.add_paragraph()
        elif t == "sign":
            d.add_paragraph()
            p = d.add_paragraph("__________________________")
            p.runs[0].font.color.rgb = INK
            lp = d.add_paragraph()
            lr = lp.add_run(b["label"])
            lr.font.size = Pt(9)
            lr.font.color.rgb = DIM
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _docx_table(d, b, INK, DIM, Pt) -> None:
    headers, rows, foot = b["headers"], b["rows"], b.get("foot") or []
    nums = _num_cols(b)
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    tbl = d.add_table(rows=1, cols=len(headers))
    tbl.style = "Light Grid Accent 1" if "Light Grid Accent 1" in [s.name for s in d.styles] else "Table Grid"
    for i, h in enumerate(headers):
        cell = tbl.rows[0].cells[i]
        run = cell.paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(9.5)
        run.font.color.rgb = INK
        if i in nums:
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for r in rows:
        cells = tbl.add_row().cells
        for i, c in enumerate(r):
            run = cells[i].paragraphs[0].add_run(c)
            run.font.size = Pt(9.5)
            run.font.color.rgb = INK
            if i in nums:
                cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for r in foot:
        cells = tbl.add_row().cells
        for i, c in enumerate(r):
            run = cells[i].paragraphs[0].add_run(c)
            run.bold = True
            run.font.size = Pt(9.5)
            run.font.color.rgb = INK
            if i in nums:
                cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT


# ── Markdown ──────────────────────────────────────────────────────────────────────

def _doc_md(ir: dict) -> str:
    out: list[str] = []
    for b in ir.get("blocks", []):
        t = b.get("t")
        if t == "title":
            out.append(f"# {b['text']}")
            if b.get("sub"):
                out.append(f"*{b['sub']}*")
        elif t == "h":
            out.append(("#" * (b.get("level", 2) + 1)) + " " + b["text"])
        elif t == "p":
            out.append(b["text"])
        elif t == "kv":
            out.extend(f"- **{k}:** {v}" for k, v in b["pairs"])
        elif t == "party":
            for side in (b["left"], b["right"]):
                if side["title"]:
                    out.append(f"**{side['title']}**")
                out.extend(side["lines"])
                out.append("")
        elif t == "table":
            out.append(_md_table(b["headers"], b["rows"] + (b.get("foot") or [])))
        elif t == "ul":
            out.extend(f"- {i}" for i in b["items"])
        elif t == "hr":
            out.append("---")
        elif t == "sign":
            out.append("")
            out.append("\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_\\_")
            out.append(b["label"])
        out.append("")
    return "\n".join(out).strip() + "\n"


def _md_table(headers: list, rows: list) -> str:
    def row(cells):
        return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"
    lines = [row(headers), "| " + " | ".join("---" for _ in headers) + " |"]
    lines += [row(r + [""] * (len(headers) - len(r))) for r in rows]
    return "\n".join(lines)


# ── Built-in PDF fallback — a valid, monochrome, paginated PDF with zero native deps ─
# Base-14 Helvetica (no embedding needed), WinAnsi text, absolute-positioned lines,
# automatic pagination and word wrap, simple heading/table/bullet layout. Not as rich
# as WeasyPrint, but a genuine, readable document that always passes %PDF validation.

_PAGE_W, _PAGE_H = 612.0, 792.0
_ML, _MR, _MT, _MB = 54.0, 54.0, 60.0, 56.0
_CONTENT_W = _PAGE_W - _ML - _MR

# Coarse Helvetica advance widths (per 1pt), enough to wrap without overflow.
_AVG = 0.50


def _text_w(s: str, size: float, bold: bool = False) -> float:
    return len(s) * size * (_AVG + (0.02 if bold else 0.0))


def _wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    for raw in str(text).split("\n"):
        words, cur = raw.split(), ""
        for w in words:
            trial = w if not cur else cur + " " + w
            if _text_w(trial, size, bold) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines or [""]


def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_enc(s: str) -> str:
    # WinAnsi/cp1252 so currency/quotes/dashes survive; unmappable → '?'.
    return _pdf_escape(s.encode("cp1252", "replace").decode("cp1252"))


class _PDFPager:
    """Accumulates absolute-positioned text ops across auto-paginated pages."""

    def __init__(self):
        self.pages: list[list[str]] = [[]]
        self.y = _PAGE_H - _MT

    def _page(self) -> list[str]:
        return self.pages[-1]

    def _newpage(self):
        self.pages.append([])
        self.y = _PAGE_H - _MT

    def space(self, h: float):
        self.y -= h
        if self.y < _MB:
            self._newpage()

    def line(self, text: str, size: float, *, bold=False, gray=False, indent=0.0, leading=1.42, gap=0.0):
        if gap:
            self.y -= gap
        wrapped = _wrap(text, size, _CONTENT_W - indent, bold)
        for ln in wrapped:
            if self.y - size < _MB:
                self._newpage()
            font = "F2" if bold else "F1"
            col = "0.42 0.44 0.47 rg\n" if gray else "0.086 0.09 0.102 rg\n"
            self._page().append(
                f"BT {col}/{font} {size:.2f} Tf 1 0 0 1 {_ML + indent:.2f} {self.y - size:.2f} Tm ({_pdf_enc(ln)}) Tj ET\n"
            )
            self.y -= size * leading

    def cols(self, cells: list[tuple[str, float, bool]], xs: list[float], size: float, *, bold=False, gray=False, gap=0.0):
        """One row of already-fitted cell strings at column x-offsets."""
        if gap:
            self.y -= gap
        if self.y - size < _MB:
            self._newpage()
        font = "F2" if bold else "F1"
        col = "0.42 0.44 0.47 rg\n" if gray else "0.086 0.09 0.102 rg\n"
        y = self.y - size
        for (text, colw, right), x in zip(cells, xs):
            s = _fit(text, size, colw, bold)
            tx = x + (colw - _text_w(s, size, bold)) if right else x
            self._page().append(f"BT {col}/{font} {size:.2f} Tf 1 0 0 1 {_ML + tx:.2f} {y:.2f} Tm ({_pdf_enc(s)}) Tj ET\n")
        self.y -= size * 1.55

    def hline(self, weight: float = 0.6, gray=True):
        self.y -= 4
        if self.y < _MB:
            self._newpage()
        col = "0.85 0.86 0.88 RG\n" if gray else "0.086 0.09 0.102 RG\n"
        self._page().append(f"{col}{weight:.2f} w {_ML:.2f} {self.y:.2f} m {_PAGE_W - _MR:.2f} {self.y:.2f} l S\n")
        self.y -= 6


def _fit(text: str, size: float, width: float, bold: bool) -> str:
    s = str(text)
    if _text_w(s, size, bold) <= width:
        return s
    while s and _text_w(s + "…", size, bold) > width:
        s = s[:-1]
    return (s + "…") if s else ""


def _builtin_pdf(ir: dict) -> bytes:
    p = _PDFPager()
    for b in ir.get("blocks", []):
        t = b.get("t")
        if t == "title":
            p.line(b["text"], 21, bold=True, gap=2)
            if b.get("sub"):
                p.line(b["sub"], 10.5, gray=True, gap=1)
            p.space(8)
        elif t == "h":
            size = {1: 13.5, 2: 11.5, 3: 10.5}[b.get("level", 2)]
            p.line(b["text"], size, bold=True, gray=b.get("level", 2) == 3, gap=10)
            p.space(2)
        elif t == "p":
            p.line(b["text"], 10.5, gap=2)
            p.space(4)
        elif t == "kv":
            for k, v in b["pairs"]:
                kw = _CONTENT_W * 0.32
                p.cols([(k, kw, False), (v, _CONTENT_W - kw, False)], [0, kw], 10, gray=False)
        elif t == "party":
            half = _CONTENT_W / 2 - 8
            left, right = b["left"], b["right"]
            p.space(4)
            if left["title"] or right["title"]:
                p.cols([(left["title"].upper(), half, False), (right["title"].upper(), half, False)],
                       [0, _CONTENT_W / 2 + 8], 8.5, bold=True, gray=True)
            for i in range(max(len(left["lines"]), len(right["lines"]))):
                lt = left["lines"][i] if i < len(left["lines"]) else ""
                rt = right["lines"][i] if i < len(right["lines"]) else ""
                p.cols([(lt, half, False), (rt, half, False)], [0, _CONTENT_W / 2 + 8], 10)
            p.space(8)
        elif t == "table":
            _builtin_table(p, b)
        elif t == "ul":
            for it in b["items"]:
                for j, ln in enumerate(_wrap(it, 10.5, _CONTENT_W - 16)):
                    p.line(("•  " if j == 0 else "   ") + ln, 10.5, indent=8, leading=1.35)
            p.space(4)
        elif t == "hr":
            p.hline()
        elif t == "sp":
            p.space(int(b.get("h", 12)))
        elif t == "sign":
            p.space(28)
            p.hline(weight=0.9, gray=False)
            p.line(b["label"], 9, gray=True)
    return _assemble_pdf(p.pages, ir.get("title") or "Document")


def _builtin_table(p: _PDFPager, b: dict) -> None:
    headers, rows, foot = b["headers"], b["rows"], b.get("foot") or []
    nums = _num_cols(b)
    n = len(headers)
    # First column takes remaining width; numeric/short columns get a fixed slice.
    fixed = min(_CONTENT_W * 0.22, 120)
    other = fixed if n > 1 else _CONTENT_W
    first = _CONTENT_W - other * (n - 1)
    widths = [first] + [other] * (n - 1)
    xs, acc = [], 0.0
    for w in widths:
        xs.append(acc)
        acc += w
    cells = [(h, widths[i] - 8, i in nums) for i, h in enumerate(headers)]
    p.space(6)
    p.cols(cells, xs, 9.5, bold=True, gap=2)
    p.hline(weight=1.2, gray=False)
    for r in rows:
        cells = [((r[i] if i < len(r) else ""), widths[i] - 8, i in nums) for i in range(n)]
        p.cols(cells, xs, 9.5)
    if foot:
        p.hline(weight=1.2, gray=False)
        for fr in foot:
            cells = [((fr[i] if i < len(fr) else ""), widths[i] - 8, i in nums) for i in range(n)]
            p.cols(cells, xs, 9.5, bold=True)
    p.space(6)


def _assemble_pdf(pages: list[list[str]], title_text: str) -> bytes:
    """Serialize positioned-text pages into a valid PDF 1.7 with an xref table.

    Object numbers are planned up front so every cross-reference is known before a
    single byte is written — no insert/renumber games:
        1 = Catalog · 2 = Pages · 3 = F1 · 4 = F2 ·
        then per page i: content = 5+2i, page = 6+2i.
    """
    n_pages = max(1, len(pages))
    page_nos = [6 + 2 * i for i in range(n_pages)]
    content_nos = [5 + 2 * i for i in range(n_pages)]
    objs: dict[int, bytes] = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{no} 0 R" for no in page_nos).encode()
    objs[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n_pages
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    for i in range(n_pages):
        ops = pages[i] if i < len(pages) else []
        # cp1252 so WinAnsi glyphs (€, curly quotes, en/em dash) survive as their byte
        # values; the fonts declare /WinAnsiEncoding so those bytes map to the right glyph.
        stream = ("".join(ops)).encode("cp1252", "replace")
        comp = zlib.compress(stream)
        objs[content_nos[i]] = (
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream")
        objs[page_nos[i]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (int(_PAGE_W), int(_PAGE_H), content_nos[i]))

    info_no = max(objs) + 1
    objs[info_no] = (b"<< /Title (" + _pdf_enc(title_text).encode("cp1252", "replace")
                     + b") /Producer (Hanzo Studio) >>")

    count = max(objs)
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (count + 1)
    for i in range(1, count + 1):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (count + 1)
    out += b"0000000000 65535 f \n"
    for i in range(1, count + 1):
        out += b"%010d 00000 n \n" % offsets[i]
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        count + 1, info_no, xref_pos)
    return bytes(out)


# ── sheet archetype: XLSX · PDF · DOCX · MD ───────────────────────────────────────
# SheetIR = {"title", "sheets": [{"name", "columns": [{"key","label","width","fmt"}],
#            "rows": [dict], "totals": [dict]}]}. fmt ∈ {"money","int","pct","date",None}.

def sheet_ir(title_text: str, sheets: list) -> dict:
    return {"title": title_text or "Spreadsheet", "sheets": sheets}


def _render_sheet(ir: dict, fmt: str) -> bytes:
    if fmt == "xlsx":
        return _sheet_xlsx(ir)
    # Secondary formats reuse the doc archetype by turning each sheet into a table block.
    d = _sheet_as_doc(ir)
    return _render_doc(d, fmt)


_XLSX_FMT = {"money": "#,##0.00", "int": "#,##0", "pct": "0.0%", "date": "yyyy-mm-dd"}


def _xlsx_safe(v):
    """OWASP formula-injection guard (CWE-1236). openpyxl turns any string cell
    starting with '=' into a LIVE formula; '+','-','@','\\t','\\r' are the other
    DDE/HYPERLINK triggers. These XLSX outputs (budgets, expense reports) are made
    to be shared, so a text cell that begins with a trigger char is coerced to text
    with a leading apostrophe — inert in Excel/Sheets, invisible to the reader.
    Numeric cells (int/float) are untouched: openpyxl only weaponizes strings."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _sheet_xlsx(ir: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    wb.remove(wb.active)
    ink = "16171A"
    head_fill = PatternFill("solid", fgColor="F0F1F3")
    head_font = Font(name="Helvetica", bold=True, color=ink, size=10)
    base_font = Font(name="Helvetica", color=ink, size=10)
    bot = Border(bottom=Side(style="thin", color="16171A"))
    thin = Border(bottom=Side(style="hair", color="D9DBDF"))
    for sh in ir.get("sheets", []):
        ws = wb.create_sheet(title=(sh.get("name") or "Sheet")[:31])
        cols = sh.get("columns", [])
        for ci, col in enumerate(cols, start=1):
            c = ws.cell(row=1, column=ci, value=col.get("label", col.get("key", "")))
            c.font = head_font
            c.fill = head_fill
            c.border = bot
            c.alignment = Alignment(horizontal="right" if col.get("fmt") in ("money", "int", "pct") else "left")
            ws.column_dimensions[c.column_letter].width = col.get("width", 18)
        r = 2
        for row in sh.get("rows", []):
            for ci, col in enumerate(cols, start=1):
                v = row.get(col["key"])
                c = ws.cell(row=r, column=ci, value=_xlsx_safe(v))
                c.font = base_font
                c.border = thin
                nf = _XLSX_FMT.get(col.get("fmt"))
                if nf:
                    c.number_format = nf
            r += 1
        for total in sh.get("totals", []):
            for ci, col in enumerate(cols, start=1):
                v = total.get(col["key"])
                c = ws.cell(row=r, column=ci, value=_xlsx_safe(v))
                c.font = Font(name="Helvetica", bold=True, color=ink, size=10)
                c.border = bot
                nf = _XLSX_FMT.get(col.get("fmt"))
                if nf and isinstance(v, (int, float)):
                    c.number_format = nf
            r += 1
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fmt_cell(v: Any, fmt: str | None) -> str:
    if v is None or v == "":
        return ""
    if fmt == "money" and isinstance(v, (int, float)):
        return f"{v:,.2f}"
    if fmt == "int" and isinstance(v, (int, float)):
        return f"{int(v):,}"
    if fmt == "pct" and isinstance(v, (int, float)):
        return f"{v * 100:.1f}%"
    return str(v)


def _sheet_as_doc(ir: dict) -> dict:
    blocks = [title(ir.get("title") or "Spreadsheet")]
    for sh in ir.get("sheets", []):
        cols = sh.get("columns", [])
        if len(ir.get("sheets", [])) > 1:
            blocks.append(heading(sh.get("name") or "Sheet", 2))
        headers = [c.get("label", c.get("key", "")) for c in cols]
        align = ["num" if c.get("fmt") in ("money", "int", "pct") else "" for c in cols]
        rows = [[_fmt_cell(row.get(c["key"]), c.get("fmt")) for c in cols] for row in sh.get("rows", [])]
        foot = [[_fmt_cell(t.get(c["key"]), c.get("fmt")) for c in cols] for t in sh.get("totals", [])]
        blocks.append(table(headers, rows, align=align, foot=foot))
    return doc_ir(ir.get("title") or "Spreadsheet", blocks)


# ── deck archetype: PPTX · PDF · MD ───────────────────────────────────────────────
# DeckIR = {"title", "slides": [{"layout": "title"|"bullets"|"section",
#           "title", "subtitle", "bullets": [str], "notes"}]}.

def deck_ir(title_text: str, slides: list) -> dict:
    return {"title": title_text or "Presentation", "slides": slides}


def _render_deck(ir: dict, fmt: str) -> bytes:
    if fmt == "pptx":
        return _deck_pptx(ir)
    return _render_doc(_deck_as_doc(ir), fmt)


def _deck_pptx(ir: dict) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    INK = RGBColor(0x16, 0x17, 0x1A)
    DIM = RGBColor(0x6B, 0x6F, 0x76)
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    def _tb(slide, left, top, width, height):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        box.text_frame.word_wrap = True
        return box.text_frame

    for sd in ir.get("slides", []):
        slide = prs.slides.add_slide(blank)
        layout = sd.get("layout", "bullets")
        if layout in ("title", "section"):
            tf = _tb(slide, 0.9, 2.6 if layout == "title" else 2.9, 11.5, 2.0)
            para0 = tf.paragraphs[0]
            run = para0.add_run()
            run.text = sd.get("title", "")
            run.font.size = Pt(40 if layout == "title" else 30)
            run.font.bold = True
            run.font.color.rgb = INK
            if sd.get("subtitle"):
                sp = tf.add_paragraph()
                sr = sp.add_run()
                sr.text = sd["subtitle"]
                sr.font.size = Pt(18)
                sr.font.color.rgb = DIM
        else:
            tf = _tb(slide, 0.9, 0.7, 11.5, 1.1)
            run = tf.paragraphs[0].add_run()
            run.text = sd.get("title", "")
            run.font.size = Pt(28)
            run.font.bold = True
            run.font.color.rgb = INK
            body = _tb(slide, 0.9, 2.0, 11.5, 4.8)
            first = True
            for b in sd.get("bullets", []):
                p = body.paragraphs[0] if first else body.add_paragraph()
                first = False
                r = p.add_run()
                r.text = str(b)
                r.font.size = Pt(18)
                r.font.color.rgb = INK
                p.space_after = Pt(8)
        if sd.get("notes"):
            slide.notes_slide.notes_text_frame.text = sd["notes"]
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _deck_as_doc(ir: dict) -> dict:
    blocks = [title(ir.get("title") or "Presentation")]
    for i, sd in enumerate(ir.get("slides", []), start=1):
        blocks.append(heading(f"{i}. {sd.get('title', '')}".strip(". "), 2))
        if sd.get("subtitle"):
            blocks.append(para(sd["subtitle"]))
        if sd.get("bullets"):
            blocks.append(bullets(sd["bullets"]))
        if sd.get("notes"):
            blocks.append(para(sd["notes"]))
    return doc_ir(ir.get("title") or "Presentation", blocks)


# ── frame archetype: HTML ─────────────────────────────────────────────────────────
# A Frames composition (hanzoai/frames). The other three archetypes hold content that a
# renderer here draws; this one holds content that is ALREADY the artifact — an HTML
# document that plays in a browser, which is the deliverable for web and for ads, and
# which `frames render` reads to produce video. So there is nothing to draw and no
# second authoring model: the composition is the source, exactly as its authors write it.
# FrameIR = {"title", "html"}.

def frame_ir(title_text: str, html: str) -> dict:
    return {"title": title_text or "Composition", "html": html or ""}


def _render_frame(ir: dict) -> bytes:
    html = ir.get("html")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("a frame is its html")
    composition(html)
    return html.encode("utf-8")


class _Root(HTMLParser):
    """The first element carrying ``data-composition-id`` — the composition's root."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs: dict | None = None

    def handle_starttag(self, tag, attrs):
        got = dict(attrs)
        if self.attrs is None and "data-composition-id" in got:
            self.attrs = got


def _positive(attrs: dict, name: str) -> float | None:
    raw = attrs.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"{name} is not a number") from None
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"{name} must be above zero")
    return val


def composition(html: str) -> dict:
    """What the document declares itself to be — and, in the same act, proof that it is
    a composition at all.

    The root ``[data-composition-id]`` element names the timeline the runtime plays, so
    an HTML file without one has nothing to play. Everything past that is checked ONLY
    when it is STATED: a length may be left off, because frames infers it from the
    timeline, and refusing that would reject valid work. A length that is stated and
    unreadable is refused, though — frames reads the root with `parseFloat` and keeps
    the result only when it is finite and non-zero, so a typo is not an error there, it
    is a silently different film. The full check is `frames lint` and `frames check`,
    which run where the renderer runs.
    """
    p = _Root()
    p.feed(html)
    p.close()
    if p.attrs is None:
        raise ValueError("no [data-composition-id] root element")
    return {"id": p.attrs.get("data-composition-id") or "",
            "width": _positive(p.attrs, "data-width"),
            "height": _positive(p.attrs, "data-height"),
            "duration": _positive(p.attrs, "data-duration")}
