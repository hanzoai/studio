"""Office documents — the built-in template catalog, AI-fill, per-org store, and REST.

The creative side of Studio is ComfyUI graphs → GPU. This is the orthogonal subsystem
for OFFICE documents: a person picks a template, describes it in one line ("Invoice for
Acme Corp, 3 line items totaling $5,000, net-30") OR fills a short form, and Studio
produces a real .pdf/.docx/.xlsx/.pptx/.md they can download. No graph, no GPU.

DRY, one source of truth per concern:
  * catalog        — one declarative CATALOG. Each template's typed ``fields`` drive the
                     form, the AI-fill JSON, AND the renderer inputs (one schema, three uses).
  * rendering      — each template ``build(fields) -> (kind, ir)`` maps fields to a neutral
                     archetype IR; ``doc_render`` owns the one renderer per format.
  * AI-fill        — description -> structured fields via the Hanzo gateway
                     (api.hanzo.ai/v1/chat/completions). AI-fill is a LAYERED ENHANCEMENT:
                     the structured-form path renders a valid document with NO LLM at all,
                     so a broken/unauthorized gateway can never block document creation.
                     Token, fail-closed and in priority order:
                       1. a server-side gateway key (STUDIO_DOC_TOKEN / STUDIO_COPILOT_TOKEN)
                          — the configured, working path; server-side only, NEVER in page
                          source. A commerce service token is a different audience and is
                          not consulted.
                       2. else the caller's own token, forwarded ONLY when it is
                          gateway-verifiable: a JWKS/asymmetric-signed JWT (RS/ES/PS/EdDSA)
                          or a publishable ``pk-`` key. A symmetric HS256 hanzo.id *session*
                          token is NEVER forwarded (the gateway rejects it: "unsupported
                          signing method: HS256"), nor is any secret ``sk-``/``hk-`` key.
                     On any gateway error the route degrades to the form — honest, never blank.
  * the document   — a stored document IS its ``(kind, ir)``. A template writes the first
                     one and is a GENERATOR, not a schema: an author — a person or an
                     agent — reads the IR, edits it, and writes it back, so a change is as
                     precise as an edit to a file and is not bounded by what some
                     template's fields can say. ``fields`` stay beside it as the form's
                     own state; PATCHing them REBUILDS the IR from the template, which is
                     the one way back to a generated document.
  * storage        — per-org tree ``orgs/<org>/documents/`` with an atomic ``index.json``
                     and one ``<id>/doc.json`` per document, tenant-scoped exactly like
                     templates and the library. doc.json is the source of truth; the files
                     are deterministic projections rendered on demand at download.

Endpoints (Hanzo /v1 house style, no /api/ prefix):
    GET    /v1/documents                       the office-template catalog (grouped)
    GET    /v1/documents/schema                the IR grammar — what an author may write
    POST   /v1/documents                       {kind, ir, title?} — author one directly
    POST   /v1/documents/generate              {templateId, description?|fields?}
    GET    /v1/documents/library               the org's generated documents
    GET    /v1/documents/{id}                  one document — its IR and its fields
    PATCH  /v1/documents/{id}                  {ir} the content, or {fields} to rebuild it
    DELETE /v1/documents/{id}                  remove a document
    GET    /v1/documents/{id}/download?format= stream the rendered file

Env (all optional):
    STUDIO_DOC_MODEL   gateway model id for AI-fill. Default "enso" (the shipped studio
                       chat default, window.HZ.model), then STUDIO_CHAT_MODEL, then "enso".
    STUDIO_CLOUD_API_URL / STUDIO_COPILOT_URL  override the gateway base (shared with gpu_dispatch).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from middleware import doc_render as R

log = logging.getLogger("studio.documents")

_MAX_DESC = 4000          # one-line-ish description cap
_MAX_FIELDS_BYTES = 256 * 1024
_MAX_IR_BYTES = 1024 * 1024   # an authored IR is content, not a form; bound it separately
_MAX_LIST = 200           # cap list/lineitem/row counts (bound render + storage)
_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")  # \Z (not $) so a trailing "\n" can't sneak past the id gate


# ── Field schema — the ONE typed vocabulary the form, the AI-fill, and build() share ─

def fd(key, label, type="text", required=False, help="", sample="", of=None):
    d = {"key": key, "label": label, "type": type, "required": required, "sample": sample}
    if help:
        d["help"] = help
    if of:
        d["of"] = of  # sub-field defs for "lineitems"/"rows"
    return d


_LIST_TYPES = ("list", "lineitems", "rows", "sections", "slides")


def _sample_of(fields):
    return {x["key"]: x.get("sample", [] if x["type"] in _LIST_TYPES else "") for x in fields}


def _num(v, default=0.0):
    try:
        if isinstance(v, str):
            v = re.sub(r"[^0-9.\-]", "", v) or "0"
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# C0 control chars are illegal in XML 1.0 (except TAB \x09, LF \x0a, CR \x0d), so
# python-docx/openpyxl/python-pptx (lxml) REJECT them — a raw \x00 in a field would
# store a doc that then 500s on docx/xlsx download (or fails generate outright). Strip
# them at this ONE coerce boundary so every renderer stays valid for any input.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value) -> str:
    return _C0_RE.sub("", "" if value is None else str(value))


def _coerce(value, ftype, of=None):
    """Coerce one field value to its declared type. Never raises — a bad value degrades
    to an empty/zero of the right shape so a hostile or sloppy AI reply can't break render."""
    if ftype in ("text", "textarea", "date", "email"):
        return _xml_text(value)
    if ftype in ("number", "money"):
        return _num(value)
    if ftype == "list":
        if isinstance(value, str):
            value = [ln for ln in re.split(r"[\n;]", value) if ln.strip()]
        return [_xml_text(x) for x in value][:_MAX_LIST] if isinstance(value, list) else []
    if ftype in ("lineitems", "rows", "sections", "slides"):
        if not isinstance(value, list):
            return []
        out = []
        for row in value[:_MAX_LIST]:
            if not isinstance(row, dict):
                continue
            out.append({sf["key"]: _coerce(row.get(sf["key"]), sf["type"]) for sf in (of or [])})
        return out
    return _xml_text(value)


def coerce_fields(tmpl, raw):
    """Project an arbitrary dict onto the template's declared fields, typed and bounded.
    Unknown keys are dropped; missing keys default empty. This is the boundary every
    field value crosses before it reaches a renderer."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for f in tmpl["fields"]:
        out[f["key"]] = _coerce(raw.get(f["key"]), f["type"], f.get("of"))
    return out


# ── Small formatting + block helpers used across builders ──────────────────────────

def _money(v, cur="$"):
    return f"{cur}{_num(v):,.2f}"


def _qty(v):
    n = _num(v)
    return str(int(n)) if n == int(n) else f"{n:g}"


def _addr_lines(*parts):
    return [p for p in parts if str(p).strip()]


def _sections_blocks(sections):
    """A list of {heading, body} → heading + paragraph blocks. Shared by report/proposal/
    SOW/one-pager/newsletter/press-release."""
    out = []
    for s in sections or []:
        h = str(s.get("heading", "")).strip()
        b = str(s.get("body", "")).strip()
        if h:
            out.append(R.heading(h, 1))
        if b:
            out.append(R.para(b))
    return out


# ── Line items (invoice / receipt) ─────────────────────────────────────────────────

_LINEITEM_OF = [
    fd("description", "Description", "text"),
    fd("qty", "Qty", "number"),
    fd("unit_price", "Unit price", "money"),
]


def _lineitem_table(items, cur="$", tax_rate=0.0, paid=None):
    rows, subtotal = [], 0.0
    for it in items:
        qty, price = _num(it.get("qty", 1)) or 1, _num(it.get("unit_price"))
        amt = qty * price
        subtotal += amt
        rows.append([str(it.get("description", "")), _qty(qty), _money(price, cur), _money(amt, cur)])
    tax = subtotal * (_num(tax_rate) / 100.0)
    total = subtotal + tax
    foot = [["", "", "Subtotal", _money(subtotal, cur)]]
    if tax:
        foot.append(["", "", f"Tax ({_qty(tax_rate)}%)", _money(tax, cur)])
    foot.append(["", "", "Total", _money(total, cur)])
    if paid is not None:
        foot.append(["", "", "Amount paid", _money(paid, cur)])
        foot.append(["", "", "Balance due", _money(total - _num(paid), cur)])
    tbl = R.table(["Description", "Qty", "Unit price", "Amount"], rows,
                  align=["", "num", "num", "num"], foot=foot)
    return tbl, total


# ── Builders: fields -> (archetype, IR) ────────────────────────────────────────────

def _b_resume(g):
    name = g("name") or "Your Name"
    blocks = [R.title(name, g("headline"))]
    contact = _addr_lines(g("email"), g("phone"), g("location"), g("links"))
    if contact:
        blocks.append(R.para("  ·  ".join(contact)))
    if g("summary"):
        blocks += [R.heading("Summary", 1), R.para(g("summary"))]
    if g("experience"):
        blocks.append(R.heading("Experience", 1))
        blocks.append(R.bullets(g("experience")))
    if g("education"):
        blocks += [R.heading("Education", 1), R.bullets(g("education"))]
    if g("skills"):
        blocks += [R.heading("Skills", 1), R.para("  ·  ".join(g("skills")))]
    return "doc", R.doc_ir(name + " — Resume", blocks)


def _b_cover_letter(g):
    blocks = [
        R.para(g("sender_name") or ""),
        R.para(g("date") or time.strftime("%B %d, %Y")),
        R.spacer(6),
    ]
    blocks += [R.party({"title": "To", "lines": _addr_lines(g("recipient_name"), g("company"), g("recipient_address"))}, {})]
    blocks += [R.para(f"Dear {g('recipient_name') or 'Hiring Manager'},"), R.para(g("body") or ""),
               R.para("Sincerely,"), R.spacer(8), R.para(g("sender_name") or "")]
    return "doc", R.doc_ir(f"Cover Letter — {g('role') or g('company') or ''}".strip(" —"), blocks)


def _b_business_letter(g):
    blocks = [R.party({"title": "From", "lines": _addr_lines(g("sender_name"), g("sender_org"), g("sender_address"))},
                      {"title": "To", "lines": _addr_lines(g("recipient_name"), g("recipient_org"), g("recipient_address"))}),
              R.para(g("date") or time.strftime("%B %d, %Y"))]
    if g("subject"):
        blocks.append(R.heading(f"Re: {g('subject')}", 2))
    blocks += [R.para(f"Dear {g('recipient_name') or 'Sir or Madam'},"), R.para(g("body") or ""),
               R.para("Sincerely,"), R.signature(g("sender_name") or "")]
    return "doc", R.doc_ir(f"Letter — {g('subject') or g('recipient_org') or ''}".strip(" —"), blocks)


def _b_memo(g):
    blocks = [R.title("MEMORANDUM"),
              R.keyvals([["To", g("to")], ["From", g("from")], ["Date", g("date") or time.strftime("%B %d, %Y")],
                         ["Re", g("subject")]]),
              R.rule(), R.para(g("body") or "")]
    return "doc", R.doc_ir(f"Memo — {g('subject') or ''}".strip(" —"), blocks)


def _b_report(g):
    blocks = [R.title(g("title") or "Report", g("subtitle"))]
    meta = _addr_lines(g("author"), g("date") or time.strftime("%B %d, %Y"))
    if meta:
        blocks.append(R.para("  ·  ".join(meta)))
    if g("summary"):
        blocks += [R.heading("Executive summary", 1), R.para(g("summary"))]
    blocks += _sections_blocks(g("sections"))
    return "doc", R.doc_ir(g("title") or "Report", blocks)


def _b_proposal(g):
    blocks = [R.title(g("title") or "Proposal", g("client") and f"Prepared for {g('client')}")]
    meta = _addr_lines(g("prepared_by"), g("date") or time.strftime("%B %d, %Y"))
    if meta:
        blocks.append(R.para("  ·  ".join(meta)))
    if g("overview"):
        blocks += [R.heading("Overview", 1), R.para(g("overview"))]
    blocks += _sections_blocks(g("sections"))
    if g("price"):
        blocks += [R.heading("Investment", 1), R.para(_money(g("price")))]
    return "doc", R.doc_ir(g("title") or "Proposal", blocks)


def _b_sow(g):
    blocks = [R.title("Statement of Work", g("project")),
              R.keyvals([["Client", g("client")], ["Provider", g("provider")],
                         ["Start", g("start")], ["End", g("end")]])]
    if g("overview"):
        blocks += [R.heading("Overview", 1), R.para(g("overview"))]
    if g("deliverables"):
        blocks += [R.heading("Deliverables", 1), R.bullets(g("deliverables"))]
    if g("milestones"):
        blocks += [R.heading("Timeline & milestones", 1), R.bullets(g("milestones"))]
    if g("fee"):
        blocks += [R.heading("Fees", 1), R.para(_money(g("fee")))]
    blocks.append(R.signature("Client signature / date"))
    return "doc", R.doc_ir(f"SOW — {g('project') or ''}".strip(" —"), blocks)


def _b_meeting_notes(g):
    blocks = [R.title(g("title") or "Meeting Notes"),
              R.keyvals([["Date", g("date") or time.strftime("%B %d, %Y")], ["Time", g("time")],
                         ["Attendees", ", ".join(g("attendees") or [])]])]
    if g("agenda"):
        blocks += [R.heading("Agenda", 1), R.bullets(g("agenda"))]
    if g("notes"):
        blocks += [R.heading("Discussion", 1), R.para(g("notes"))]
    if g("action_items"):
        blocks += [R.heading("Action items", 1), R.bullets(g("action_items"))]
    return "doc", R.doc_ir(g("title") or "Meeting Notes", blocks)


def _b_invoice(g):
    cur = g("currency") or "$"
    tbl, total = _lineitem_table(g("items") or [], cur, g("tax_rate"))
    num = g("number") or "INV-1001"
    blocks = [
        R.title("INVOICE", num),
        R.party({"title": "From", "lines": _addr_lines(g("from_name"), g("from_email"), g("from_address"))},
                {"title": "Bill to", "lines": _addr_lines(g("to_name"), g("to_email"), g("to_address"))}),
        R.keyvals([["Invoice #", num], ["Date", g("date") or time.strftime("%Y-%m-%d")],
                   ["Due", g("due")], ["Terms", g("terms")]]),
        tbl,
    ]
    if g("notes"):
        blocks += [R.heading("Notes", 2), R.para(g("notes"))]
    return "doc", R.doc_ir(f"Invoice {num}", blocks)


def _b_receipt(g):
    cur = g("currency") or "$"
    tbl, total = _lineitem_table(g("items") or [], cur, g("tax_rate"), paid=g("amount_paid") or None)
    num = g("number") or "RC-1001"
    blocks = [
        R.title("RECEIPT", num),
        R.party({"title": "From", "lines": _addr_lines(g("from_name"), g("from_address"))},
                {"title": "Received from", "lines": _addr_lines(g("to_name"))}),
        R.keyvals([["Receipt #", num], ["Date", g("date") or time.strftime("%Y-%m-%d")],
                   ["Payment method", g("method")]]),
        tbl,
    ]
    return "doc", R.doc_ir(f"Receipt {num}", blocks)


def _b_contract(g):
    blocks = [R.title(g("title") or "Agreement"),
              R.para(f"This Agreement is entered into on {g('date') or time.strftime('%B %d, %Y')} "
                     f"by and between {g('party_a') or '[Party A]'} and {g('party_b') or '[Party B]'}.")]
    blocks += _sections_blocks(g("clauses"))
    blocks += [R.spacer(10),
               R.party({"title": g("party_a") or "Party A", "lines": ["", "Signature", "Date"]},
                       {"title": g("party_b") or "Party B", "lines": ["", "Signature", "Date"]})]
    return "doc", R.doc_ir(g("title") or "Agreement", blocks)


def _b_press_release(g):
    blocks = [R.para("FOR IMMEDIATE RELEASE"), R.title(g("headline") or "Announcement", g("subhead"))]
    dateline = _addr_lines(g("location"), g("date") or time.strftime("%B %d, %Y"))
    lead = f"{' — '.join(dateline)} — " if dateline else ""
    blocks.append(R.para(lead + (g("body") or "")))
    if g("boilerplate"):
        blocks += [R.heading(f"About {g('company') or ''}".strip(), 2), R.para(g("boilerplate"))]
    if g("contact"):
        blocks += [R.heading("Media contact", 2), R.para(g("contact"))]
    blocks.append(R.para("###"))
    return "doc", R.doc_ir(g("headline") or "Press Release", blocks)


def _b_newsletter(g):
    blocks = [R.title(g("title") or "Newsletter", g("date") or time.strftime("%B %Y"))]
    if g("intro"):
        blocks.append(R.para(g("intro")))
    blocks += _sections_blocks(g("sections"))
    if g("footer"):
        blocks += [R.rule(), R.para(g("footer"))]
    return "doc", R.doc_ir(g("title") or "Newsletter", blocks)


def _b_one_pager(g):
    blocks = [R.title(g("title") or "One-pager", g("tagline"))]
    if g("intro"):
        blocks.append(R.para(g("intro")))
    if g("highlights"):
        blocks += [R.heading("Highlights", 1), R.bullets(g("highlights"))]
    blocks += _sections_blocks(g("sections"))
    if g("cta"):
        blocks += [R.rule(), R.para(g("cta"))]
    return "doc", R.doc_ir(g("title") or "One-pager", blocks)


def _b_certificate(g):
    blocks = [R.spacer(30), R.title("Certificate of " + (g("kind") or "Achievement")),
              R.spacer(10), R.para("This certifies that"),
              R.heading(g("recipient") or "Recipient Name", 1),
              R.para(g("description") or "has successfully completed the requirements."),
              R.spacer(24),
              R.party({"title": g("issuer") or "Issued by", "lines": ["", "Signature"]},
                      {"title": "Date", "lines": [g("date") or time.strftime("%B %d, %Y")]})]
    return "doc", R.doc_ir(f"Certificate — {g('recipient') or ''}".strip(" —"), blocks)


# sheet builders ---------------------------------------------------------------------

def _grid_sheet(title_text, sheet_name, columns, rows, total_key=None, total_label="Total"):
    """columns = [{key,label,fmt?}]. Sum the money/number columns for a totals row when
    ``total_key`` (a text column to hold the label) is given."""
    totals = []
    if total_key:
        tot = {total_key: total_label}
        for c in columns:
            if c.get("fmt") in ("money", "int", "number") and c["key"] != total_key:
                tot[c["key"]] = sum(_num(r.get(c["key"])) for r in rows)
        totals = [tot]
    return "sheet", R.sheet_ir(title_text, [{"name": sheet_name, "columns": columns, "rows": rows, "totals": totals}])


def _b_budget(g):
    cols = [{"key": "category", "label": "Category"}, {"key": "planned", "label": "Planned", "fmt": "money"},
            {"key": "actual", "label": "Actual", "fmt": "money"}]
    rows = [{"category": r.get("category"), "planned": _num(r.get("planned")), "actual": _num(r.get("actual"))}
            for r in (g("rows") or [])]
    return _grid_sheet(g("title") or "Budget", g("period") or "Budget", cols, rows, total_key="category")


def _b_expense_tracker(g):
    cols = [{"key": "date", "label": "Date"}, {"key": "vendor", "label": "Vendor"},
            {"key": "category", "label": "Category"}, {"key": "amount", "label": "Amount", "fmt": "money"}]
    rows = [{"date": r.get("date"), "vendor": r.get("vendor"), "category": r.get("category"),
             "amount": _num(r.get("amount"))} for r in (g("rows") or [])]
    return _grid_sheet(g("title") or "Expenses", "Expenses", cols, rows, total_key="date", total_label="Total")


def _b_timesheet(g):
    cols = [{"key": "date", "label": "Date"}, {"key": "task", "label": "Task"},
            {"key": "hours", "label": "Hours", "fmt": "number"}]
    rows = [{"date": r.get("date"), "task": r.get("task"), "hours": _num(r.get("hours"))}
            for r in (g("rows") or [])]
    name = f"Timesheet — {g('name')}".strip(" —") if g("name") else "Timesheet"
    return _grid_sheet(name, "Timesheet", cols, rows, total_key="date", total_label="Total")


def _b_project_plan(g):
    cols = [{"key": "task", "label": "Task"}, {"key": "owner", "label": "Owner"},
            {"key": "start", "label": "Start"}, {"key": "due", "label": "Due"},
            {"key": "status", "label": "Status"}]
    rows = [{"task": r.get("task"), "owner": r.get("owner"), "start": r.get("start"),
             "due": r.get("due"), "status": r.get("status")} for r in (g("rows") or [])]
    return _grid_sheet(g("title") or "Project Plan", "Plan", cols, rows)


# deck builders ----------------------------------------------------------------------

def _slides_from(g, opening_subtitle=""):
    slides = [{"layout": "title", "title": g("title") or "Presentation", "subtitle": opening_subtitle or g("subtitle") or ""}]
    for s in (g("slides") or []):
        slides.append({"layout": "bullets", "title": str(s.get("heading", "")),
                       "bullets": s.get("bullets") if isinstance(s.get("bullets"), list) else
                       [x for x in re.split(r"[\n;]", str(s.get("bullets", ""))) if x.strip()],
                       "notes": str(s.get("notes", ""))})
    return slides


def _b_pitch_deck(g):
    return "deck", R.deck_ir(g("title") or "Pitch", _slides_from(g, g("company") and f"{g('company')} — {g('tagline') or ''}".strip(" —")))


def _b_business_review(g):
    return "deck", R.deck_ir(g("title") or "Business Review", _slides_from(g, g("period") or ""))


def _b_project_update(g):
    return "deck", R.deck_ir(g("title") or "Project Update", _slides_from(g, g("period") or ""))


# ── The catalog — grouped, declarative, DRY ────────────────────────────────────────

def _t(id, category, name, icon, description, formats, fields, build):
    return {"id": id, "category": category, "name": name, "icon": icon, "description": description,
            "formats": formats, "fields": fields, "build": build, "sample": _sample_of(fields)}


_SECTION_OF = [fd("heading", "Heading", "text"), fd("body", "Body", "textarea")]
_SLIDE_OF = [fd("heading", "Slide title", "text"), fd("bullets", "Bullets", "list"), fd("notes", "Speaker notes", "textarea")]

CATALOG = [
    # ── Documents (PDF · DOCX · MD) ──
    _t("resume", "Documents", "Resume / CV", "📄", "A clean professional resume", ["pdf", "docx", "md"], [
        fd("name", "Full name", "text", True, sample="Ada Lovelace"),
        fd("headline", "Headline", "text", sample="Mathematician & First Programmer"),
        fd("email", "Email", "email", sample="ada@analytical.engine"),
        fd("phone", "Phone", "text", sample="+44 20 7946 0000"),
        fd("location", "Location", "text", sample="London, UK"),
        fd("links", "Links", "text", sample="lovelace.dev"),
        fd("summary", "Summary", "textarea", sample="Pioneering mathematician who wrote the first published algorithm intended for a machine."),
        fd("experience", "Experience", "list", sample=["Analytical Engine — authored Note G, the first algorithm (1843)", "Translated & annotated Menabrea's memoir, tripling its length"]),
        fd("education", "Education", "list", sample=["Private tutelage in mathematics under Augustus De Morgan"]),
        fd("skills", "Skills", "list", sample=["Algorithms", "Mathematics", "Technical writing"]),
    ], _b_resume),
    _t("cover_letter", "Documents", "Cover Letter", "✉️", "A tailored job cover letter", ["pdf", "docx", "md"], [
        fd("sender_name", "Your name", "text", True, sample="Ada Lovelace"),
        fd("recipient_name", "Recipient", "text", sample="Hiring Manager"),
        fd("company", "Company", "text", sample="Analytical Engines Ltd"),
        fd("recipient_address", "Company address", "text", sample="London, UK"),
        fd("role", "Role", "text", sample="Lead Programmer"),
        fd("date", "Date", "date", sample=""),
        fd("body", "Letter body", "textarea", True, sample="I am writing to express my strong interest in the Lead Programmer role. My work on the Analytical Engine demonstrates exactly the rigor and vision your team needs."),
    ], _b_cover_letter),
    _t("business_letter", "Documents", "Business Letter", "🖋", "A formal business letter", ["pdf", "docx", "md"], [
        fd("sender_name", "Sender", "text", True, sample="Jane Doe"),
        fd("sender_org", "Sender org", "text", sample="Acme Corp"),
        fd("sender_address", "Sender address", "text", sample="100 Market St, San Francisco, CA"),
        fd("recipient_name", "Recipient", "text", sample="John Smith"),
        fd("recipient_org", "Recipient org", "text", sample="Globex"),
        fd("recipient_address", "Recipient address", "text", sample="200 Broadway, New York, NY"),
        fd("subject", "Subject", "text", sample="Partnership proposal"),
        fd("date", "Date", "date", sample=""),
        fd("body", "Body", "textarea", True, sample="We are pleased to propose a strategic partnership between our organizations, outlined in the attached terms."),
    ], _b_business_letter),
    _t("memo", "Documents", "Memo", "📝", "An internal memorandum", ["pdf", "docx", "md"], [
        fd("to", "To", "text", sample="All Staff"),
        fd("from", "From", "text", sample="Operations"),
        fd("date", "Date", "date", sample=""),
        fd("subject", "Subject", "text", True, sample="Updated remote-work policy"),
        fd("body", "Body", "textarea", True, sample="Effective next month, the office adopts a hybrid schedule of three in-office days per week. Details and FAQs follow."),
    ], _b_memo),
    _t("report", "Documents", "Report", "📊", "A structured business report", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", True, sample="Q3 Performance Report"),
        fd("subtitle", "Subtitle", "text", sample="Revenue, growth, and outlook"),
        fd("author", "Author", "text", sample="Finance Team"),
        fd("date", "Date", "date", sample=""),
        fd("summary", "Executive summary", "textarea", sample="Revenue grew 24% quarter-over-quarter, driven by enterprise expansion and improved retention."),
        fd("sections", "Sections", "sections", sample=[
            {"heading": "Revenue", "body": "Total revenue reached $4.2M, up 24% QoQ."},
            {"heading": "Growth drivers", "body": "Enterprise deals and net-revenue retention above 118%."},
            {"heading": "Outlook", "body": "We project continued double-digit growth into Q4."},
        ], of=_SECTION_OF),
    ], _b_report),
    _t("proposal", "Documents", "Proposal", "📑", "A client project proposal", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", True, sample="Website Redesign Proposal"),
        fd("client", "Client", "text", sample="Globex Corp"),
        fd("prepared_by", "Prepared by", "text", sample="Acme Studio"),
        fd("date", "Date", "date", sample=""),
        fd("overview", "Overview", "textarea", sample="A complete redesign of the Globex marketing site to lift conversion and modernize the brand."),
        fd("sections", "Sections", "sections", sample=[
            {"heading": "Scope", "body": "Discovery, design system, 12 templates, and CMS integration."},
            {"heading": "Timeline", "body": "Eight weeks from kickoff to launch."},
        ], of=_SECTION_OF),
        fd("price", "Price", "money", sample=48000),
    ], _b_proposal),
    _t("sow", "Documents", "Statement of Work", "📋", "A statement of work", ["pdf", "docx", "md"], [
        fd("project", "Project", "text", True, sample="Mobile App v2"),
        fd("client", "Client", "text", sample="Globex Corp"),
        fd("provider", "Provider", "text", sample="Acme Studio"),
        fd("start", "Start date", "date", sample="2026-08-01"),
        fd("end", "End date", "date", sample="2026-11-01"),
        fd("overview", "Overview", "textarea", sample="Design and build v2 of the Globex mobile app for iOS and Android."),
        fd("deliverables", "Deliverables", "list", sample=["UX research report", "Design system", "iOS + Android apps", "QA + launch support"]),
        fd("milestones", "Milestones", "list", sample=["M1 — Research (wk 2)", "M2 — Design (wk 5)", "M3 — Build (wk 10)", "M4 — Launch (wk 13)"]),
        fd("fee", "Fee", "money", sample=120000),
    ], _b_sow),
    _t("meeting_notes", "Documents", "Meeting Notes", "🗒", "Minutes with action items", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", sample="Weekly Sync"),
        fd("date", "Date", "date", sample=""),
        fd("time", "Time", "text", sample="10:00–10:45"),
        fd("attendees", "Attendees", "list", sample=["Ada", "Grace", "Alan"]),
        fd("agenda", "Agenda", "list", sample=["Sprint review", "Roadmap", "Blockers"]),
        fd("notes", "Discussion", "textarea", sample="Reviewed sprint progress; two items slipped to next week. Aligned on Q4 roadmap priorities."),
        fd("action_items", "Action items", "list", sample=["Ada to finalize the API spec by Fri", "Grace to schedule the design review"]),
    ], _b_meeting_notes),
    _t("invoice", "Documents", "Invoice", "🧾", "Bill a client with line items", ["pdf", "docx", "md"], [
        fd("number", "Invoice #", "text", sample="INV-1001"),
        fd("from_name", "From", "text", True, sample="Acme Studio"),
        fd("from_email", "From email", "email", sample="billing@acme.studio"),
        fd("from_address", "From address", "text", sample="100 Market St, San Francisco, CA"),
        fd("to_name", "Bill to", "text", True, sample="Globex Corp"),
        fd("to_email", "Bill-to email", "email", sample="ap@globex.com"),
        fd("to_address", "Bill-to address", "text", sample="200 Broadway, New York, NY"),
        fd("date", "Date", "date", sample=""),
        fd("due", "Due date", "date", sample=""),
        fd("terms", "Terms", "text", sample="Net 30"),
        fd("currency", "Currency symbol", "text", sample="$"),
        fd("tax_rate", "Tax rate (%)", "number", sample=0),
        fd("items", "Line items", "lineitems", True, sample=[
            {"description": "Brand & design system", "qty": 1, "unit_price": 2000},
            {"description": "Website build", "qty": 1, "unit_price": 2500},
            {"description": "Content & QA", "qty": 1, "unit_price": 500},
        ], of=_LINEITEM_OF),
        fd("notes", "Notes", "textarea", sample="Payment via bank transfer. Thank you for your business."),
    ], _b_invoice),
    _t("receipt", "Documents", "Receipt", "🧾", "A payment receipt", ["pdf", "docx", "md"], [
        fd("number", "Receipt #", "text", sample="RC-2001"),
        fd("from_name", "From", "text", True, sample="Acme Studio"),
        fd("from_address", "From address", "text", sample="100 Market St, San Francisco, CA"),
        fd("to_name", "Received from", "text", True, sample="Globex Corp"),
        fd("date", "Date", "date", sample=""),
        fd("method", "Payment method", "text", sample="Visa ••4242"),
        fd("currency", "Currency symbol", "text", sample="$"),
        fd("tax_rate", "Tax rate (%)", "number", sample=0),
        fd("amount_paid", "Amount paid", "money", sample=5000),
        fd("items", "Line items", "lineitems", True, sample=[
            {"description": "Consulting — August", "qty": 40, "unit_price": 125},
        ], of=_LINEITEM_OF),
    ], _b_receipt),
    _t("contract", "Documents", "Contract / Agreement", "⚖️", "A simple agreement", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", True, sample="Services Agreement"),
        fd("party_a", "Party A", "text", True, sample="Acme Studio LLC"),
        fd("party_b", "Party B", "text", True, sample="Globex Corp"),
        fd("date", "Date", "date", sample=""),
        fd("clauses", "Clauses", "sections", sample=[
            {"heading": "1. Services", "body": "Party A shall provide the services described in the attached Statement of Work."},
            {"heading": "2. Payment", "body": "Party B shall pay all invoices within thirty (30) days of receipt."},
            {"heading": "3. Term", "body": "This Agreement remains in effect until the services are complete or terminated with 30 days' notice."},
            {"heading": "4. Confidentiality", "body": "Each party shall keep the other's confidential information secret."},
        ], of=_SECTION_OF),
    ], _b_contract),
    _t("press_release", "Documents", "Press Release", "📣", "An announcement for the press", ["pdf", "docx", "md"], [
        fd("headline", "Headline", "text", True, sample="Acme Launches Studio, an AI Document Platform"),
        fd("subhead", "Subhead", "text", sample="One-line descriptions become polished documents in seconds"),
        fd("location", "Location", "text", sample="San Francisco, CA"),
        fd("date", "Date", "date", sample=""),
        fd("company", "Company", "text", sample="Acme"),
        fd("body", "Body", "textarea", True, sample="Acme today announced Studio, a platform that turns a one-line description into a finished, downloadable office document. Early customers report producing invoices and proposals in under a minute."),
        fd("boilerplate", "About the company", "textarea", sample="Acme builds AI tools that make professional work effortless."),
        fd("contact", "Media contact", "text", sample="press@acme.studio · +1 415 555 0100"),
    ], _b_press_release),
    _t("newsletter", "Documents", "Newsletter", "🗞", "A simple newsletter", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", True, sample="The Acme Monthly"),
        fd("date", "Issue date", "text", sample=""),
        fd("intro", "Intro", "textarea", sample="Welcome to this month's edition — product news, a customer story, and what's next."),
        fd("sections", "Stories", "sections", sample=[
            {"heading": "Product news", "body": "We shipped office documents: create a PDF or Word file from one line."},
            {"heading": "Customer story", "body": "Globex cut proposal turnaround from days to minutes."},
        ], of=_SECTION_OF),
        fd("footer", "Footer", "text", sample="You're receiving this because you subscribed at acme.studio."),
    ], _b_newsletter),
    _t("one_pager", "Documents", "One-pager / Flyer", "📃", "A single-page overview", ["pdf", "docx", "md"], [
        fd("title", "Title", "text", True, sample="Acme Studio"),
        fd("tagline", "Tagline", "text", sample="Documents, done in one line"),
        fd("intro", "Intro", "textarea", sample="Studio turns a plain-language description into a finished, downloadable document."),
        fd("highlights", "Highlights", "list", sample=["22 professional templates", "PDF, Word, Excel, PowerPoint", "AI-filled from one sentence"]),
        fd("sections", "Sections", "sections", sample=[{"heading": "How it works", "body": "Pick a template, describe it, download. Under 30 seconds."}], of=_SECTION_OF),
        fd("cta", "Call to action", "text", sample="Start creating at studio.hanzo.ai"),
    ], _b_one_pager),
    _t("certificate", "Documents", "Certificate", "🏅", "A certificate of completion", ["pdf", "docx", "md"], [
        fd("kind", "Type", "text", sample="Completion"),
        fd("recipient", "Recipient", "text", True, sample="Grace Hopper"),
        fd("description", "Description", "textarea", sample="has successfully completed the Advanced Systems Programming course."),
        fd("issuer", "Issued by", "text", sample="Acme Academy"),
        fd("date", "Date", "date", sample=""),
    ], _b_certificate),
    # ── Spreadsheets (XLSX · PDF · DOCX · MD) ──
    _t("budget", "Spreadsheets", "Budget", "💰", "Planned vs. actual budget", ["xlsx", "pdf", "docx", "md"], [
        fd("title", "Title", "text", sample="2026 Budget"),
        fd("period", "Period", "text", sample="FY2026"),
        fd("rows", "Rows", "rows", True, sample=[
            {"category": "Salaries", "planned": 240000, "actual": 232000},
            {"category": "Marketing", "planned": 60000, "actual": 71000},
            {"category": "Software", "planned": 24000, "actual": 21500},
            {"category": "Office", "planned": 36000, "actual": 34000},
        ], of=[fd("category", "Category", "text"), fd("planned", "Planned", "money"), fd("actual", "Actual", "money")]),
    ], _b_budget),
    _t("expense_tracker", "Spreadsheets", "Expense Tracker", "🧮", "Track expenses with a total", ["xlsx", "pdf", "docx", "md"], [
        fd("title", "Title", "text", sample="Expenses — August"),
        fd("rows", "Expenses", "rows", True, sample=[
            {"date": "2026-08-02", "vendor": "AWS", "category": "Software", "amount": 1240.55},
            {"date": "2026-08-09", "vendor": "WeWork", "category": "Office", "amount": 3000},
            {"date": "2026-08-15", "vendor": "Delta", "category": "Travel", "amount": 612.4},
        ], of=[fd("date", "Date", "date"), fd("vendor", "Vendor", "text"), fd("category", "Category", "text"), fd("amount", "Amount", "money")]),
    ], _b_expense_tracker),
    _t("timesheet", "Spreadsheets", "Timesheet", "⏱", "Hours by day and task", ["xlsx", "pdf", "docx", "md"], [
        fd("name", "Name", "text", sample="Ada Lovelace"),
        fd("rows", "Entries", "rows", True, sample=[
            {"date": "2026-08-03", "task": "API design", "hours": 6},
            {"date": "2026-08-04", "task": "Implementation", "hours": 8},
            {"date": "2026-08-05", "task": "Code review", "hours": 3},
        ], of=[fd("date", "Date", "date"), fd("task", "Task", "text"), fd("hours", "Hours", "number")]),
    ], _b_timesheet),
    _t("project_plan", "Spreadsheets", "Project Plan", "🗂", "Tasks, owners, dates, status", ["xlsx", "pdf", "docx", "md"], [
        fd("title", "Title", "text", sample="Launch Plan"),
        fd("rows", "Tasks", "rows", True, sample=[
            {"task": "Research", "owner": "Ada", "start": "2026-08-01", "due": "2026-08-14", "status": "Done"},
            {"task": "Design", "owner": "Grace", "start": "2026-08-15", "due": "2026-09-05", "status": "In progress"},
            {"task": "Build", "owner": "Alan", "start": "2026-09-06", "due": "2026-10-10", "status": "Not started"},
        ], of=[fd("task", "Task", "text"), fd("owner", "Owner", "text"), fd("start", "Start", "date"), fd("due", "Due", "date"), fd("status", "Status", "text")]),
    ], _b_project_plan),
    # ── Presentations (PPTX · PDF · MD) ──
    _t("pitch_deck", "Presentations", "Pitch Deck", "🚀", "An investor pitch deck", ["pptx", "pdf", "md"], [
        fd("title", "Title", "text", True, sample="Acme"),
        fd("company", "Company", "text", sample="Acme, Inc."),
        fd("tagline", "Tagline", "text", sample="Documents, done in one line"),
        fd("slides", "Slides", "slides", True, sample=[
            {"heading": "Problem", "bullets": ["Business docs are slow to make", "Templates are rigid", "AI tools don't produce real files"], "notes": "Open with the pain."},
            {"heading": "Solution", "bullets": ["One line → a finished document", "PDF, Word, Excel, PowerPoint", "Editable, on-brand, instant"]},
            {"heading": "Market", "bullets": ["$40B document software market", "Every knowledge worker", "Bottoms-up adoption"]},
            {"heading": "Ask", "bullets": ["Raising $3M seed", "18-month runway", "Go-to-market + model quality"]},
        ], of=_SLIDE_OF),
    ], _b_pitch_deck),
    _t("business_review", "Presentations", "Business Review", "📈", "A QBR / business review deck", ["pptx", "pdf", "md"], [
        fd("title", "Title", "text", True, sample="Q3 Business Review"),
        fd("period", "Period", "text", sample="Q3 2026"),
        fd("slides", "Slides", "slides", True, sample=[
            {"heading": "Highlights", "bullets": ["Revenue +24% QoQ", "NRR 118%", "Two enterprise logos"]},
            {"heading": "Metrics", "bullets": ["ARR $16.8M", "Gross margin 82%", "CAC payback 11 months"]},
            {"heading": "Risks", "bullets": ["Hiring behind plan", "One large renewal at risk"]},
            {"heading": "Next quarter", "bullets": ["Ship self-serve", "Expand EMEA", "Close Series A"]},
        ], of=_SLIDE_OF),
    ], _b_business_review),
    _t("project_update", "Presentations", "Project Update", "📌", "A status update deck", ["pptx", "pdf", "md"], [
        fd("title", "Title", "text", True, sample="Mobile App v2 — Update"),
        fd("period", "As of", "text", sample="Week 6"),
        fd("slides", "Slides", "slides", True, sample=[
            {"heading": "Status", "bullets": ["On track for Nov 1 launch", "Design complete", "Build 60% done"]},
            {"heading": "Done this week", "bullets": ["Auth flow", "Offline sync", "Push notifications"]},
            {"heading": "Next week", "bullets": ["Payments", "QA pass 1", "Beta invites"]},
            {"heading": "Risks", "bullets": ["App Store review timing", "One dependency upgrade pending"]},
        ], of=_SLIDE_OF),
    ], _b_project_update),
]

_BY_ID = {t["id"]: t for t in CATALOG}
CATEGORY_ORDER = ("Documents", "Spreadsheets", "Presentations")


def get_template(tid):
    return _BY_ID.get(tid)


def _public_field(f):
    out = {k: f[k] for k in ("key", "label", "type", "required") if k in f}
    for k in ("help", "of", "sample"):
        if f.get(k) not in (None, ""):
            out[k] = f[k]
    return out


def public_template(t):
    """Catalog entry without the (unserializable) build fn — the wire shape."""
    return {
        "id": t["id"], "category": t["category"], "name": t["name"], "icon": t["icon"],
        "description": t["description"], "formats": t["formats"],
        "fields": [_public_field(f) for f in t["fields"]], "sample": t["sample"],
    }


def catalog_grouped():
    groups = {c: [] for c in CATEGORY_ORDER}
    for t in CATALOG:
        groups.setdefault(t["category"], []).append(public_template(t))
    return [{"name": c, "templates": groups[c]} for c in CATEGORY_ORDER if groups.get(c)]


# ── Rendering a stored document ────────────────────────────────────────────────────

def build_ir(tmpl, fields):
    """(archetype kind, IR) for a template + coerced fields. Pure — used by render and tests."""
    g = coerce_fields(tmpl, fields).get
    return tmpl["build"](g)


def render_document(tmpl, fields, fmt):
    kind, ir = build_ir(tmpl, fields)
    return R.render(kind, ir, fmt), ir.get("title") or tmpl["name"]


def stored_ir(doc):
    """``(kind, ir)`` for a stored document, or ``(None, None)`` when neither is reachable.

    A document written before documents carried their own IR holds only
    ``{templateId, fields}``, so its template is asked. The answer is not written back —
    a read never mutates the store, and the rebuild is what PATCHing ``fields`` does.
    """
    kind, ir = doc.get("kind"), doc.get("ir")
    if kind in R.KIND_FORMATS and isinstance(ir, dict):
        return kind, ir
    tmpl = get_template(doc.get("templateId"))
    if tmpl is None:
        return None, None
    return build_ir(tmpl, doc.get("fields") or {})


def render_stored(doc, fmt):
    """Render a stored document. Raises ``LookupError`` when it has no reachable IR."""
    kind, ir = stored_ir(doc)
    if ir is None:
        raise LookupError("no IR and no template")
    return R.render(kind, ir, fmt), ir.get("title") or doc.get("title") or "Document"


# Markdown is the check because it is the one format every archetype renders from the
# standard library alone. Rendering the PRIMARY format instead asks a second question —
# is python-pptx installed here — and answers "this document is invalid" when the honest
# answer is "this pod cannot draw that one format".
_PROOF_FORMAT = "md"


def prove(kind, ir):
    """Render ``ir`` once so nothing unrenderable is ever stored. Raises on refusal."""
    R.render(kind, ir, _PROOF_FORMAT)


def read_ir(body, kind_hint=None):
    """The authored ``(kind, ir)`` in a request body, validated. Raises ``ValueError``.

    An IR is data, so the check is its shape and its size — what it MEANS is settled by
    rendering it, which every write does before it stores anything.
    """
    ir = body.get("ir")
    if not isinstance(ir, dict):
        raise ValueError("ir must be an object")
    if len(json.dumps(ir)) > _MAX_IR_BYTES:
        raise ValueError("ir too large")
    kind = body.get("kind") or kind_hint
    if kind not in R.KIND_FORMATS:
        raise ValueError(f"kind must be one of {sorted(R.KIND_FORMATS)}")
    return kind, ir


# ── AI-fill: description -> structured fields via the Hanzo gateway ─────────────────

# AI-gateway server keys only — a commerce service token is a different audience (it used
# to be listed here and would 502 every AI-fill; see gateway_auth).
_SERVER_TOKEN_ENV = ("STUDIO_DOC_TOKEN", "STUDIO_COPILOT_TOKEN")


def _ai_token(request):
    """The bearer AI-fill sends to the gateway, "" when none is usable (→ form fallback).

    Delegates to the ONE canonical fail-closed allowlist (``middleware.gateway_auth``) —
    NO private copy (they used to drift: the same crafted-alg hardening had to be applied
    to both). A server-side gateway key wins (never in page source); else the caller's own
    token ONLY when the gateway can verify it (a ``pk-`` key or an asymmetric/JWKS JWT) —
    an HS256 session token and ``sk-``/``hk-``/``rk-`` secrets are refused."""
    from middleware import gateway_auth  # lazy: keeps this module import light
    return gateway_auth.ai_bearer(request, server_env=_SERVER_TOKEN_ENV)


def _gateway_url():
    base = (os.environ.get("STUDIO_COPILOT_URL", "").strip()
            or (os.environ.get("STUDIO_CLOUD_API_URL", "").rstrip("/") + "/v1/chat/completions")
            or "https://api.hanzo.ai/v1/chat/completions")
    return base


def _gateway_model():
    # Enso is the shipped studio default (window.HZ.model); STUDIO_DOC_MODEL overrides.
    return os.environ.get("STUDIO_DOC_MODEL", "").strip() or os.environ.get("STUDIO_CHAT_MODEL", "").strip() or "enso"


_JSON_TYPE = {"text": "string", "textarea": "string", "date": "string", "email": "string",
              "number": "number", "money": "number"}


def _field_schema(f):
    t = f["type"]
    if t in _JSON_TYPE:
        return {"type": _JSON_TYPE[t]}
    if t == "list":
        return {"type": "array", "items": {"type": "string"}}
    if t in ("lineitems", "rows", "sections", "slides"):
        of = f.get("of") or (_SECTION_OF if t == "sections" else _SLIDE_OF if t == "slides" else _LINEITEM_OF)
        return {"type": "array", "items": {"type": "object",
                "properties": {sf["key"]: _field_schema(sf) for sf in of}}}
    return {"type": "string"}


def _fill_tool(tmpl):
    props = {f["key"]: dict(_field_schema(f), description=f["label"]) for f in tmpl["fields"]}
    return {"type": "function", "function": {
        "name": "fill_document",
        "description": f"Fill the fields of a '{tmpl['name']}' document from the user's description.",
        "parameters": {"type": "object", "properties": {"fields": {"type": "object", "properties": props}},
                       "required": ["fields"]}}}


def _fill_messages(tmpl, description):
    schema = {f["key"]: f["type"] for f in tmpl["fields"]}
    sys = (
        f"You fill office-document templates. The document is a {tmpl['name']}: {tmpl['description']}.\n"
        f"Return ONLY the fill_document tool call. Field keys and their types:\n{json.dumps(schema)}\n"
        "Rules: use the user's facts; invent nothing material (names, amounts, dates) that isn't implied. "
        "For money/number fields return raw numbers (no currency symbols or commas). For list fields return "
        "an array of short strings. For lineitems return objects with description, qty, unit_price. Keep prose "
        "concise and professional. Leave a field empty if the user gives nothing for it."
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": description[:_MAX_DESC]}]


def parse_fill(completion, tmpl):
    """Pull the ``fields`` object from an OpenAI-style completion (tool_call preferred,
    then embedded JSON), coerced to the template schema. Returns {} when nothing usable —
    the caller then falls back to the provided form fields."""
    try:
        msg = completion["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {}
    raw = None
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name") == "fill_document":
            try:
                raw = json.loads(fn.get("arguments") or "{}").get("fields")
            except (json.JSONDecodeError, TypeError):
                raw = None
            break
    if raw is None:
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        s, e = content.find("{"), content.rfind("}")
        if s != -1 and e > s:
            try:
                obj = json.loads(content[s:e + 1])
                raw = obj.get("fields", obj)
            except json.JSONDecodeError:
                raw = None
    if not isinstance(raw, dict):
        return {}
    return coerce_fields(tmpl, raw)


class AIFillError(Exception):
    pass


async def ai_fill(request, tmpl, description):
    """Description -> coerced fields via the gateway, forwarding the caller's own token.
    Raises AIFillError with an honest reason (no token, gateway down) so the route can
    fall back to the form instead of a blank."""
    token = _ai_token(request)
    if not token:
        raise AIFillError("sign in to use AI fill — or fill the form fields")
    payload = {"model": _gateway_model(), "messages": _fill_messages(tmpl, description),
               "tools": [_fill_tool(tmpl)], "tool_choice": "auto", "temperature": 0.2}
    headers = {"Content-Type": "application/json", "Authorization": token}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
            async with s.post(_gateway_url(), json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise AIFillError(f"AI fill unavailable ({resp.status})")
                data = json.loads(text)
    except aiohttp.ClientError as e:
        raise AIFillError("AI fill unreachable") from e
    except json.JSONDecodeError as e:
        raise AIFillError("AI fill returned bad data") from e
    return parse_fill(data, tmpl)


# ── Per-org store — atomic, tenant-scoped, mirrors templates/library ────────────────

def _docs_root(org):
    import folder_paths
    out = Path(folder_paths.get_org_output_directory(org))
    base = out.parent if out.parent.name == org else out
    d = base / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_atomic(path, obj):
    fd_, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd_, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_index(org):
    f = _docs_root(org) / "index.json"
    if f.is_file():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {"documents": []}


def _save_index(org, idx):
    _write_atomic(_docs_root(org) / "index.json", idx)


def _doc_dir(org, doc_id):
    return _docs_root(org) / doc_id


def _load_doc(org, doc_id):
    if not _ID_RE.match(doc_id or ""):
        return None
    f = _doc_dir(org, doc_id) / "doc.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _save_doc(org, doc):
    d = _doc_dir(org, doc["id"])
    d.mkdir(parents=True, exist_ok=True)
    _write_atomic(d / "doc.json", doc)


def _index_entry(doc):
    return {"id": doc["id"], "templateId": doc["templateId"], "title": doc["title"],
            "category": doc.get("category", ""), "icon": doc.get("icon", ""),
            "formats": doc["formats"], "createdAt": doc["createdAt"], "updatedAt": doc["updatedAt"]}


def _upsert_index(org, doc):
    idx = _load_index(org)
    docs = [d for d in idx.get("documents", []) if d.get("id") != doc["id"]]
    docs.insert(0, _index_entry(doc))
    _save_index(org, {"documents": docs})


def _slugify(s):
    return (re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip()).strip("-").lower() or "document")[:60]


# ── Routes ─────────────────────────────────────────────────────────────────────────

def add_documents_routes(routes: web.RouteTableDef, server, *, org_of, owner_of=None):
    """Register the office-document endpoints. Tenancy resolvers are injected (the same
    ``_org_of``/``_owner_of`` the home routes use) so this subsystem stays decoupled —
    exactly the pattern stacks_store follows."""

    def _download_urls(doc):
        return {fmt: f"/v1/documents/{doc['id']}/download?format={fmt}" for fmt in doc["formats"]}

    @routes.get("/v1/documents")
    async def documents_catalog(request: web.Request):
        return web.json_response({"categories": catalog_grouped()})

    @routes.get("/v1/documents/schema")
    async def documents_schema(request: web.Request):
        """What an author may write. Registered ahead of ``/{id}`` so the word wins."""
        return web.json_response({"archetypes": R.grammar()})

    @routes.post("/v1/documents")
    async def documents_author(request: web.Request):
        """Author a document directly from its IR — no template, no fields, no LLM.

        A template is a generator, so this is the same door with the generator skipped:
        anything the renderers can draw can be written here, which is the whole reason an
        author is not bounded by the 22 shapes the catalog happens to ship.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "object body required"}, status=400)
        try:
            kind, ir = read_ir(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        formats = list(R.KIND_FORMATS[kind])
        try:
            prove(kind, ir)
        except Exception as e:
            log.warning("documents.author render failed: %s", e)
            return web.json_response({"error": "could not render this document"}, status=422)

        org = org_of(request)
        now = int(time.time())
        title = (body.get("title") or "").strip() or ir.get("title") or "Document"
        doc = {"id": uuid.uuid4().hex, "templateId": "", "title": title,
               "category": "Documents", "icon": "\U0001F4C4", "formats": formats,
               "kind": kind, "ir": ir, "fields": {}, "createdAt": now, "updatedAt": now,
               "createdBy": (owner_of(request) if owner_of else "")}
        _save_doc(org, doc)
        _upsert_index(org, doc)
        return web.json_response({"id": doc["id"], "title": title, "kind": kind, "ir": ir,
                                  "formats": formats, "download": _download_urls(doc)})

    @routes.post("/v1/documents/generate")
    async def documents_generate(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "object body required"}, status=400)
        tmpl = get_template(body.get("templateId"))
        if tmpl is None:
            return web.json_response({"error": "unknown templateId"}, status=404)

        description = (body.get("description") or "").strip()
        form_fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
        if len(json.dumps(form_fields)) > _MAX_FIELDS_BYTES:
            return web.json_response({"error": "fields too large"}, status=413)
        if not description and not form_fields:
            return web.json_response({"error": "provide a description or fields"}, status=400)

        note = ""
        fields = {}
        if description:
            try:
                fields = await ai_fill(request, tmpl, description)
            except AIFillError as e:
                if not form_fields:
                    # Honest fail-closed: no AI and nothing to render from.
                    return web.json_response(
                        {"error": str(e), "needFields": True,
                         "template": public_template(tmpl)}, status=422)
                note = str(e)
        # Explicit form fields always win over AI-inferred ones.
        merged = {**fields, **form_fields}
        merged = coerce_fields(tmpl, merged)

        # Render once — never store a document nothing can draw.
        try:
            kind, ir = build_ir(tmpl, merged)
            prove(kind, ir)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("documents.generate render failed: %s", e)
            return web.json_response({"error": "could not render this document"}, status=500)
        title = ir.get("title") or tmpl["name"]

        org = org_of(request)
        now = int(time.time())
        doc = {"id": uuid.uuid4().hex, "templateId": tmpl["id"], "title": title,
               "category": tmpl["category"], "icon": tmpl["icon"], "formats": tmpl["formats"],
               "kind": kind, "ir": ir,
               "fields": merged, "createdAt": now, "updatedAt": now,
               "createdBy": (owner_of(request) if owner_of else "")}
        _save_doc(org, doc)
        _upsert_index(org, doc)
        resp = {"id": doc["id"], "templateId": tmpl["id"], "title": title, "category": tmpl["category"],
                "icon": tmpl["icon"], "formats": tmpl["formats"], "fields": merged,
                "download": _download_urls(doc)}
        if note:
            resp["note"] = note
        return web.json_response(resp)

    @routes.get("/v1/documents/library")
    async def documents_library(request: web.Request):
        idx = _load_index(org_of(request))
        return web.json_response({"documents": idx.get("documents", [])},
                                 headers={"Cache-Control": "no-store"})

    @routes.get("/v1/documents/{id}")
    async def documents_get(request: web.Request):
        doc = _load_doc(org_of(request), request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        doc = dict(doc)
        doc["download"] = _download_urls(doc)
        tmpl = get_template(doc.get("templateId"))
        doc["template"] = public_template(tmpl) if tmpl else None
        # An older document has no IR of its own; derive it so every reader sees the same
        # shape and an agent never has to know which generation wrote it.
        kind, ir = stored_ir(doc)
        doc["kind"], doc["ir"] = kind, ir
        return web.json_response(doc)

    @routes.patch("/v1/documents/{id}")
    async def documents_update(request: web.Request):
        org = org_of(request)
        doc = _load_doc(org, request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "object body required"}, status=400)
        tmpl = get_template(doc.get("templateId"))

        if body.get("ir") is not None:
            # The content itself. It wins over fields in the same body: the fields wrote
            # the IR once, and after an edit they no longer describe it.
            try:
                kind, ir = read_ir(body, kind_hint=doc.get("kind") or stored_ir(doc)[0])
                prove(kind, ir)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                log.warning("documents.update render failed: %s", e)
                return web.json_response({"error": "could not render this document"}, status=422)
            doc["kind"], doc["ir"] = kind, ir
        elif isinstance(body.get("fields"), dict):
            # Rebuilding from the template is the one way back to a generated document,
            # and it DISCARDS an authored IR — which is what "regenerate" means.
            if tmpl is None:
                return web.json_response({"error": "template retired"}, status=410)
            doc["fields"] = coerce_fields(tmpl, {**doc.get("fields", {}), **body["fields"]})
            doc["kind"], doc["ir"] = build_ir(tmpl, doc["fields"])

        ir = doc.get("ir") or {}
        asked = (body.get("title") or "").strip()
        doc["title"] = asked or ir.get("title") or doc["title"]
        doc["updatedAt"] = int(time.time())
        _save_doc(org, doc)
        _upsert_index(org, doc)
        return web.json_response({"id": doc["id"], "title": doc["title"], "kind": doc.get("kind"),
                                  "ir": doc.get("ir"), "fields": doc.get("fields", {}),
                                  "download": _download_urls(doc)})

    @routes.delete("/v1/documents/{id}")
    async def documents_delete(request: web.Request):
        org = org_of(request)
        doc_id = request.match_info["id"]
        if not _ID_RE.match(doc_id or ""):
            return web.json_response({"error": "not found"}, status=404)
        idx = _load_index(org)
        remaining = [d for d in idx.get("documents", []) if d.get("id") != doc_id]
        _save_index(org, {"documents": remaining})
        d = _doc_dir(org, doc_id)
        try:
            if (d / "doc.json").exists():
                (d / "doc.json").unlink()
            if d.exists():
                d.rmdir()
        except OSError:
            pass
        return web.json_response({"ok": True, "id": doc_id})

    @routes.get("/v1/documents/{id}/download")
    async def documents_download(request: web.Request):
        org = org_of(request)
        doc = _load_doc(org, request.match_info["id"])
        if doc is None:
            return web.json_response({"error": "not found"}, status=404)
        fmt = (request.query.get("format") or doc["formats"][0]).lower()
        if fmt not in doc["formats"]:
            return web.json_response({"error": f"format must be one of {doc['formats']}"}, status=400)
        try:
            data, title = render_stored(doc, fmt)
        except LookupError:
            return web.json_response({"error": "template retired"}, status=410)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("documents.download render failed: %s", e)
            return web.json_response({"error": "could not render this document"}, status=500)
        filename = f"{_slugify(title)}.{fmt}"
        return web.Response(body=data, headers={
            "Content-Type": R.content_type(fmt),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        })
