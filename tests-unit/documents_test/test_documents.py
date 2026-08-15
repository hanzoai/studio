"""Office documents — the non-GPU document pipeline.

Covers the four concerns end to end:
  * catalog        — every template well-formed, formats within archetype capability,
                     wire shape JSON-serializable (no build fn leaks).
  * render         — schema -> IR -> real bytes for EVERY template in EVERY declared
                     format, asserted by magic numbers (%PDF, OOXML zip, markdown).
  * AI-fill parse  — a mocked gateway completion -> coerced, typed fields; the
                     forwardable-bearer allowlist is fail-closed (JWT/pk- only).
  * REST + tenancy — generate/library/get/patch/delete/download over a real aiohttp app,
                     per-org isolation, honest 4xx, correct Content-Type/-Disposition.

Runs without a GPU, without torch, and without the WeasyPrint native stack (the built-in
PDF fallback produces a valid %PDF), so it is safe for the dependency-light CI unit gate.
"""
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

# PDF (built-in fallback) and Markdown are pure-stdlib and always render. DOCX/XLSX/PPTX
# need their wheel; when it is absent (a bare env) we SKIP that format instead of failing —
# the canonical gate (requirements.txt installed) exercises every format for real.
_FMT_LIB = {"docx": "docx", "xlsx": "openpyxl", "pptx": "pptx"}


def _fmt_available(fmt: str) -> bool:
    lib = _FMT_LIB.get(fmt)
    return lib is None or importlib.util.find_spec(lib) is not None

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

import folder_paths
from middleware import documents as D
from middleware import doc_render as R


# ── helpers ────────────────────────────────────────────────────────────────────────

def _valid(fmt: str, b: bytes) -> bool:
    if fmt == "pdf":
        return b[:5] == b"%PDF-" and b"%%EOF" in b[-64:]
    if fmt == "html":
        # A composition has no magic bytes; what makes it valid is that it declares a
        # root the runtime can play, which is the same question `composition()` asks.
        try:
            return bool(R.composition(b.decode("utf-8"))["id"])
        except (UnicodeDecodeError, ValueError):
            return False
    if fmt == "md":
        try:
            return len(b) > 10 and bool(b.decode("utf-8").strip())  # real, non-empty UTF-8 text
        except UnicodeDecodeError:
            return False
    try:
        return zipfile.ZipFile(io.BytesIO(b)).testzip() is None
    except zipfile.BadZipFile:
        return False


def _jwt(alg: str) -> str:
    """A JWT with the given header alg (payload/sig are inert — only the header matters
    to the forwardable-bearer allowlist)."""
    import base64
    h = base64.urlsafe_b64encode(json.dumps({"alg": alg, "typ": "JWT"}).encode()).rstrip(b"=").decode()
    return f"{h}.eyJzdWIiOiIxIn0.sig"


_OOXML_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}


# ── catalog ─────────────────────────────────────────────────────────────────────────

def test_catalog_has_all_common_templates():
    ids = {t["id"] for t in D.CATALOG}
    expected = {
        # Documents
        "resume", "cover_letter", "business_letter", "memo", "report", "proposal", "sow",
        "meeting_notes", "invoice", "receipt", "contract", "press_release", "newsletter",
        "one_pager", "certificate",
        # Spreadsheets
        "budget", "expense_tracker", "timesheet", "project_plan",
        # Presentations
        "pitch_deck", "business_review", "project_update",
    }
    assert expected <= ids, f"missing templates: {expected - ids}"
    assert len(D.CATALOG) == len(ids), "duplicate template ids"


def test_catalog_invariants():
    for t in D.CATALOG:
        assert t["category"] in D.CATEGORY_ORDER
        assert t["formats"], f"{t['id']} has no formats"
        # every template's declared formats are within its archetype's capability
        kind, _ir = D.build_ir(t, t["sample"])
        assert set(t["formats"]) <= set(R.KIND_FORMATS[kind]), \
            f"{t['id']} ({kind}) declares formats outside {R.KIND_FORMATS[kind]}"
        # the primary (first) format is the archetype's native format
        assert t["formats"][0] == R.KIND_FORMATS[kind][0]
        # fields typed and keyed
        for f in t["fields"]:
            assert f["key"] and f["label"] and f["type"]


def test_public_catalog_is_json_serializable_without_build_fn():
    grouped = D.catalog_grouped()
    blob = json.dumps({"categories": grouped})  # must not raise (build fn stripped)
    assert '"build"' not in blob
    names = [g["name"] for g in grouped]
    assert names == [c for c in D.CATEGORY_ORDER]  # grouped in the canonical order
    total = sum(len(g["templates"]) for g in grouped)
    assert total == len(D.CATALOG)


# ── render matrix: every template × every declared format ───────────────────────────

@pytest.mark.parametrize("tmpl", D.CATALOG, ids=[t["id"] for t in D.CATALOG])
def test_every_template_renders_every_format(tmpl):
    for fmt in tmpl["formats"]:
        if not _fmt_available(fmt):
            continue  # wheel not installed here; the canonical gate covers it
        data, title = D.render_document(tmpl, tmpl["sample"], fmt)
        assert isinstance(data, (bytes, bytearray)) and data, f"{tmpl['id']}/{fmt} produced no bytes"
        assert _valid(fmt, data), f"{tmpl['id']}/{fmt} invalid magic bytes"
        if fmt in _OOXML_PART:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            assert _OOXML_PART[fmt] in names, f"{tmpl['id']}/{fmt} missing {_OOXML_PART[fmt]}"
        assert title


def test_pdf_fallback_is_a_valid_pdf_without_weasyprint():
    # The built-in writer must stand alone (CI/pods without pango). Assert directly.
    ir = R.doc_ir("T", [R.title("Hello"), R.para("World " * 40),
                        R.table(["A", "B"], [["1", "2"], ["3", "4"]], foot=[["", "sum"]]),
                        R.bullets(["one", "two"]), R.signature("sig")])
    pdf = R._builtin_pdf(ir)
    assert pdf[:5] == b"%PDF-" and b"%%EOF" in pdf[-64:]
    assert pdf.count(b" obj") == pdf.count(b"endobj")  # balanced objects
    assert b"/Root 1 0 R" in pdf and b"startxref" in pdf


def test_invoice_math_flows_into_the_document():
    inv = D.get_template("invoice")
    fields = dict(inv["sample"], tax_rate=10, items=[
        {"description": "A", "qty": 2, "unit_price": 100},   # 200
        {"description": "B", "qty": 1, "unit_price": 50},    # 50  -> subtotal 250, tax 25, total 275
    ])
    md = D.render_document(inv, fields, "md")[0].decode()
    assert "Subtotal" in md and "250.00" in md
    assert "275.00" in md  # total = subtotal + 10% tax


# ── AI-fill: parse + coercion + fail-closed bearer allowlist ────────────────────────

def test_parse_fill_from_tool_call_coerces_types():
    inv = D.get_template("invoice")
    completion = {"choices": [{"message": {"tool_calls": [{"function": {
        "name": "fill_document",
        "arguments": json.dumps({"fields": {
            "number": "INV-9", "to_name": "Acme Corp", "tax_rate": "8.5",
            "items": [{"description": "Consulting", "qty": "3", "unit_price": "$1,000"}],
            "bogus_key": "dropped",
        }}),
    }}]}}]}
    fields = D.parse_fill(completion, inv)
    assert fields["number"] == "INV-9"
    assert fields["tax_rate"] == 8.5 and isinstance(fields["tax_rate"], float)
    assert fields["items"][0] == {"description": "Consulting", "qty": 3.0, "unit_price": 1000.0}
    assert "bogus_key" not in fields                       # unknown keys dropped
    assert "from_name" in fields                           # every declared field present


def test_parse_fill_from_embedded_json_content():
    memo = D.get_template("memo")
    completion = {"choices": [{"message": {"content":
        'Here you go: {"fields": {"subject": "Policy", "body": "Text"}}'}}]}
    fields = D.parse_fill(completion, memo)
    assert fields["subject"] == "Policy" and fields["body"] == "Text"


def test_parse_fill_tolerates_garbage():
    memo = D.get_template("memo")
    assert D.parse_fill({}, memo) == {}
    assert D.parse_fill({"choices": [{"message": {"content": "no json here"}}]}, memo) == {}


class _Req:
    def __init__(self, auth=None, cookies=None):
        self.headers = {"Authorization": auth} if auth else {}
        self.cookies = cookies or {}


def test_forwardable_bearer_is_fail_closed(monkeypatch):
    for env in D._SERVER_TOKEN_ENV:
        monkeypatch.delenv(env, raising=False)
    rs, hs = _jwt("RS256"), _jwt("HS256")
    assert D._ai_token(_Req("Bearer " + rs)) == "Bearer " + rs   # JWKS-verifiable JWT
    assert D._ai_token(_Req("Bearer " + hs)) == ""               # HS256 session token REFUSED
    assert D._ai_token(_Req("Bearer pk-publishable")) == "Bearer pk-publishable"
    assert D._ai_token(_Req("Bearer sk-secret")) == ""           # secret keys refused
    assert D._ai_token(_Req("Bearer hk-secret")) == ""
    assert D._ai_token(_Req("Bearer rk-secret")) == ""
    assert D._ai_token(_Req(None)) == ""                          # no token
    assert D._ai_token(_Req("Bearer notoken")) == ""              # opaque, not JWT/pk-
    # session-cookie path (what the browser sends) is honored for a verifiable JWT
    assert D._ai_token(_Req(None, {"access_token": rs})) == "Bearer " + rs


def test_ai_token_prefers_server_gateway_key(monkeypatch):
    for env in D._SERVER_TOKEN_ENV:
        monkeypatch.delenv(env, raising=False)
    hs = _jwt("HS256")
    # No server key + only an HS256 session token → no usable AI token (fail-closed → form).
    assert D._ai_token(_Req("Bearer " + hs)) == ""
    # A configured server gateway key (server-side only, never page source) is used and wins.
    monkeypatch.setenv("STUDIO_DOC_TOKEN", "hk-server-key")
    assert D._ai_token(_Req("Bearer " + hs)) == "Bearer hk-server-key"



# ── REST surface over a real aiohttp app ────────────────────────────────────────────
# Driven on a self-managed event loop so these are plain SYNC tests — no dependency on
# the pytest-asyncio / pytest-aiohttp async-mode configuration (which varies across
# environments). One loop per test; the app + client live and die with the fixture.

import asyncio

from aiohttp.test_utils import TestClient, TestServer


class _Resp:
    """A fully-materialized response (body read eagerly) with sync accessors."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def json(self):
        return json.loads(self._body)

    def read(self):
        return self._body

    def text(self):
        return self._body.decode("utf-8", "replace")


class _SyncClient:
    def __init__(self, tc, loop):
        self._tc = tc
        self._loop = loop

    def _request(self, method, url, **kw):
        async def go():
            r = await self._tc.request(method, url, **kw)
            body = await r.read()
            return _Resp(r.status, dict(r.headers), body)
        return self._loop.run_until_complete(go())

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def patch(self, url, **kw):
        return self._request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._request("DELETE", url, **kw)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A live app with the documents routes and a tmp per-org tree. ``X-Test-Org`` header
    selects the tenant (the injected org_of), so one client exercises multi-tenancy."""
    def outd(org=None):
        d = tmp_path / "orgs" / (org or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(folder_paths, "get_org_output_directory", outd)

    def org_of(request):
        return request.headers.get("X-Test-Org", "default")

    def owner_of(request):
        return request.headers.get("X-Test-User", "")

    app = web.Application()
    routes = web.RouteTableDef()
    D.add_documents_routes(routes, object(), org_of=org_of, owner_of=owner_of)
    app.add_routes(routes)

    loop = asyncio.new_event_loop()
    tc = TestClient(TestServer(app), loop=loop)
    loop.run_until_complete(tc.start_server())
    try:
        yield _SyncClient(tc, loop)
    finally:
        loop.run_until_complete(tc.close())
        loop.close()


def _gen(client, template_id, **body):
    body["templateId"] = template_id
    return client.post("/v1/documents/generate", json=body)


def test_catalog_endpoint(client):
    r = client.get("/v1/documents")
    assert r.status == 200
    cats = {g["name"] for g in r.json()["categories"]}
    assert {"Documents", "Spreadsheets", "Presentations"} <= cats


def test_generate_from_fields_then_download_pdf_and_docx(client):
    """The guarantee: a document renders end-to-end with NO LLM — form fields only."""
    inv = D.get_template("invoice")
    r = _gen(client, "invoice", fields=inv["sample"])
    assert r.status == 200, r.text()
    doc = r.json()
    assert doc["id"] and doc["title"].startswith("Invoice")
    assert set(doc["download"]) == set(inv["formats"])

    fmts = [("pdf", "application/pdf")]
    if _fmt_available("docx"):
        fmts.append(("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    for fmt, want_ct in fmts:
        dl = client.get(doc["download"][fmt])
        assert dl.status == 200
        assert dl.headers["Content-Type"] == want_ct
        assert "attachment;" in dl.headers["Content-Disposition"]
        assert dl.headers["Content-Disposition"].endswith(f'.{fmt}"')
        assert _valid(fmt, dl.read())


def test_generate_each_archetype_native_format(client):
    for tid, fmt in (("resume", "pdf"), ("budget", "xlsx"), ("pitch_deck", "pptx")):
        if not _fmt_available(fmt):
            continue
        t = D.get_template(tid)
        doc = _gen(client, tid, fields=t["sample"]).json()
        dl = client.get(doc["download"][fmt])
        assert dl.status == 200
        assert dl.headers["Content-Type"] == R.content_type(fmt)
        assert _valid(fmt, dl.read())


def test_generate_uses_ai_fill_when_description_given(client, monkeypatch):
    # Mock the gateway call: description -> canned fields (no network).
    async def fake_ai_fill(request, tmpl, description):
        assert "Acme" in description
        return D.coerce_fields(tmpl, {"to_name": "Acme Corp", "number": "INV-42",
                                      "items": [{"description": "Work", "qty": 1, "unit_price": 5000}]})
    monkeypatch.setattr(D, "ai_fill", fake_ai_fill)

    doc = _gen(client, "invoice", description="Invoice for Acme Corp, $5,000, net-30").json()
    assert doc["fields"]["to_name"] == "Acme Corp"
    assert doc["title"] == "Invoice INV-42"
    md = client.get(doc["download"]["md"]).read()
    assert b"5,000.00" in md and b"Acme Corp" in md


def test_explicit_fields_override_ai(client, monkeypatch):
    async def fake_ai_fill(request, tmpl, description):
        return D.coerce_fields(tmpl, {"subject": "AI subject", "body": "AI body"})
    monkeypatch.setattr(D, "ai_fill", fake_ai_fill)
    doc = _gen(client, "memo", description="anything", fields={"subject": "Human subject"}).json()
    assert doc["fields"]["subject"] == "Human subject"   # explicit wins
    assert doc["fields"]["body"] == "AI body"            # AI fills the gap


def test_generate_fail_closed_without_token_or_fields(client, monkeypatch):
    # description given, but no server key and no forwardable token -> AI unavailable, and
    # no form fields to fall back to: honest 422 asking for fields, never a blank doc.
    for env in D._SERVER_TOKEN_ENV:
        monkeypatch.delenv(env, raising=False)
    r = _gen(client, "invoice", description="Invoice for Acme")
    assert r.status == 422
    j = r.json()
    assert j["needFields"] is True and j["template"]["id"] == "invoice"


def test_generate_rejects_unknown_template_and_empty_body(client):
    assert _gen(client, "nope", fields={}).status == 404
    assert client.post("/v1/documents/generate", json={"templateId": "memo"}).status == 400


def test_library_lists_generated_docs_newest_first(client):
    _gen(client, "memo", fields={"subject": "One", "body": "b"})
    _gen(client, "resume", fields=D.get_template("resume")["sample"])
    docs = client.get("/v1/documents/library").json()["documents"]
    assert len(docs) == 2
    assert docs[0]["templateId"] == "resume"   # newest first
    assert {"id", "title", "templateId", "formats", "createdAt"} <= set(docs[0])


def test_get_patch_reflected_in_download(client):
    doc_id = _gen(client, "memo", fields={"subject": "Before", "body": "b"}).json()["id"]
    got = client.get(f"/v1/documents/{doc_id}").json()
    assert got["fields"]["subject"] == "Before"
    assert got["template"]["id"] == "memo"

    assert client.patch(f"/v1/documents/{doc_id}", json={"fields": {"subject": "After"}}).status == 200
    md = client.get(f"/v1/documents/{doc_id}/download?format=md").read()
    assert b"After" in md and b"Before" not in md


def test_delete_removes_document(client):
    doc_id = _gen(client, "memo", fields={"subject": "x", "body": "b"}).json()["id"]
    assert client.delete(f"/v1/documents/{doc_id}").status == 200
    assert client.get(f"/v1/documents/{doc_id}").status == 404
    assert client.get("/v1/documents/library").json()["documents"] == []


def test_tenancy_isolation_between_orgs(client):
    doc_id = client.post("/v1/documents/generate",
                         json={"templateId": "memo", "fields": {"subject": "secret"}},
                         headers={"X-Test-Org": "org-a"}).json()["id"]
    # org-b cannot see or download org-a's document
    assert client.get(f"/v1/documents/{doc_id}", headers={"X-Test-Org": "org-b"}).status == 404
    assert client.get(f"/v1/documents/{doc_id}/download?format=pdf",
                      headers={"X-Test-Org": "org-b"}).status == 404
    assert client.get("/v1/documents/library", headers={"X-Test-Org": "org-b"}).json()["documents"] == []
    # org-a still has it
    assert client.get(f"/v1/documents/{doc_id}", headers={"X-Test-Org": "org-a"}).status == 200


def test_download_rejects_bad_id_and_format(client):
    assert client.get("/v1/documents/not-a-real-id/download").status == 404
    doc_id = _gen(client, "memo", fields={"subject": "x", "body": "b"}).json()["id"]
    assert client.get(f"/v1/documents/{doc_id}/download?format=xlsx").status == 400  # not in memo formats


# ── RED-review regressions (@e631d35f): XLSX formula injection, C0 control chars, crafted JWT alg ──

def test_xlsx_text_cell_formula_is_neutralized():
    # the guard: a formula-trigger text cell becomes inert text; numbers are untouched.
    assert R._xlsx_safe("=1+1") == "'=1+1"
    assert R._xlsx_safe("+a") == "'+a" and R._xlsx_safe("-b") == "'-b" and R._xlsx_safe("@c") == "'@c"
    assert R._xlsx_safe("plain") == "plain" and R._xlsx_safe(42) == 42 and R._xlsx_safe(3.5) == 3.5
    if not _fmt_available("xlsx"):
        pytest.skip("openpyxl not installed")
    import openpyxl
    tmpl = next(t for t in D.CATALOG if t["id"] == "budget")
    fields = {k: (list(v) if isinstance(v, list) else v) for k, v in tmpl["sample"].items()}
    rows = [dict(r) for r in fields.get("rows", [])] or [dict.fromkeys(("category", "planned", "actual"), "")]
    first_key = next(iter(rows[0]), "category")
    rows[0][first_key] = "=cmd|'/c calc.exe'!A1"
    fields["rows"] = rows
    data, _ = D.render_document(tmpl, fields, "xlsx")
    ws = openpyxl.load_workbook(io.BytesIO(data)).worksheets[0]
    cells = [c for row in ws.iter_rows() for c in row]
    assert all(c.data_type != "f" for c in cells), "no cell may be a live formula"
    hit = [c for c in cells if isinstance(c.value, str) and "calc.exe" in c.value]
    assert hit and all(not c.value.startswith("=") for c in hit), "the =-leading value was neutralized to text"


def test_control_chars_stripped_and_ooxml_stays_valid():
    # C0 controls (illegal in XML 1.0) are stripped at the coerce boundary; \t \n \r survive.
    assert D._coerce("a\x00b\x0b\x1fc", "text") == "abc"
    assert D._coerce("keep\ttab\nnl\rcr", "textarea") == "keep\ttab\nnl\rcr"
    # end-to-end: a NUL/BEL in a field renders valid docx/pdf/md/xlsx — never a 500 / poison doc.
    memo = next(t for t in D.CATALOG if t["id"] == "memo")
    mf = dict(memo["sample"])
    mf["body"] = "line1\x00\x07line2"
    for fmt in [f for f in ("pdf", "docx", "md") if _fmt_available(f)]:
        data, _ = D.render_document(memo, mf, fmt)
        assert _valid(fmt, data), f"{fmt} must be valid after control-char strip"
    if _fmt_available("xlsx"):
        b = next(t for t in D.CATALOG if t["id"] == "budget")
        bf = {k: (list(v) if isinstance(v, list) else v) for k, v in b["sample"].items()}
        rows = [dict(r) for r in bf.get("rows", [])] or [{"category": "", "planned": 0, "actual": 0}]
        rows[0][next(iter(rows[0]), "category")] = "x\x00y"
        bf["rows"] = rows
        assert _valid("xlsx", D.render_document(b, bf, "xlsx")[0])


def test_ai_token_delegates_to_canonical_gateway_auth(monkeypatch):
    """After the decomplect, documents._ai_token routes through the ONE gateway_auth
    allowlist (no private copy — they used to drift). Prove the fail-closed behavior via
    the public _ai_token: a crafted non-string alg must not 500; HS256/secret refused;
    pk-/asymmetric forwarded; a server key wins."""
    for env in D._SERVER_TOKEN_ENV:
        monkeypatch.delenv(env, raising=False)

    def jwt_with_alg(alg_value):  # a JWT header carrying an arbitrary (maybe non-string) alg
        import base64
        h = base64.urlsafe_b64encode(json.dumps({"alg": alg_value}).encode()).rstrip(b"=").decode()
        return f"{h}.eyJzdWIiOiIxIn0.sig"

    for bad in (None, [], 123, {"x": 1}, True):
        assert D._ai_token(_Req("Bearer " + jwt_with_alg(bad))) == ""      # crafted alg → no 500
    assert D._ai_token(_Req("Bearer " + jwt_with_alg("HS256"))) == ""      # HS256 session token refused
    assert D._ai_token(_Req("Bearer sk-secret")) == ""                     # secret refused
    assert D._ai_token(_Req("Bearer pk-x")) == "Bearer pk-x"               # publishable forwarded
    assert D._ai_token(_Req("Bearer " + jwt_with_alg("RS256"))) == "Bearer " + jwt_with_alg("RS256")
    monkeypatch.setenv("STUDIO_DOC_TOKEN", "sk-server")                    # server key wins
    assert D._ai_token(_Req("Bearer pk-x")) == "Bearer sk-server"


# ── The IR is the document ─────────────────────────────────────────────────────────
# A template writes the first one and is a generator, not a schema. What follows is the
# consequence: an author reads the IR, edits it, and writes it back, and the file that
# comes out is the edit.

def test_schema_names_every_block_the_renderer_draws(client):
    """The published grammar and the doc renderer cannot drift.

    The grammar exists so an author is accurate instead of guessing, which is worth
    nothing if it names a block the renderer ignores — an agent would write it, get a
    200, and download a file with the block silently missing.
    """
    import inspect
    import re as _re
    drawn = set(_re.findall(r't == "([a-z]+)"', inspect.getsource(R._doc_html_body)))
    assert drawn == set(R.GRAMMAR["doc"]["blocks"]), drawn ^ set(R.GRAMMAR["doc"]["blocks"])

    r = client.get("/v1/documents/schema")
    assert r.status == 200
    arch = r.json()["archetypes"]
    assert set(arch) == set(R.KIND_FORMATS)
    assert arch["deck"]["formats"] == ["pptx", "pdf", "md"]


def test_author_a_document_the_catalog_cannot_express(client):
    """No template, no fields, no LLM — the IR alone."""
    ir = R.deck_ir("Series A", [
        {"layout": "title", "title": "Series A", "subtitle": "Hanzo"},
        {"layout": "bullets", "title": "Why now", "bullets": ["One", "Two", "Three"]},
    ])
    r = client.post("/v1/documents", json={"kind": "deck", "ir": ir})
    assert r.status == 200, r.text()
    doc = r.json()
    assert doc["kind"] == "deck" and doc["title"] == "Series A"
    assert doc["formats"] == ["pptx", "pdf", "md"]

    md = client.get(doc["download"]["md"]).read().decode()
    assert "Why now" in md and "Three" in md


def test_editing_the_ir_changes_the_file(client):
    doc = _gen(client, "pitch_deck", fields=D.get_template("pitch_deck")["sample"]).json()
    ir = client.get(f"/v1/documents/{doc['id']}").json()["ir"]
    ir["slides"].append({"layout": "bullets", "title": "Pricing", "bullets": ["$20 a seat"]})

    r = client.patch(f"/v1/documents/{doc['id']}", json={"ir": ir})
    assert r.status == 200, r.text()
    md = client.get(doc["download"]["md"]).read().decode()
    assert "Pricing" in md and "$20 a seat" in md


def test_patching_fields_rebuilds_from_the_template(client):
    """The one way back to a generated document — and it discards the authored IR,
    which is what "regenerate" means."""
    doc = _gen(client, "memo", fields=D.get_template("memo")["sample"]).json()
    ir = client.get(f"/v1/documents/{doc['id']}").json()["ir"]
    ir["blocks"].append(R.para("An authored line."))
    client.patch(f"/v1/documents/{doc['id']}", json={"ir": ir})
    assert "An authored line." in client.get(doc["download"]["md"]).read().decode()

    r = client.patch(f"/v1/documents/{doc['id']}", json={"fields": {"subject": "Rebuilt"}})
    assert r.status == 200
    md = client.get(doc["download"]["md"]).read().decode()
    assert "Rebuilt" in md and "An authored line." not in md


def test_a_retired_template_no_longer_orphans_a_document(client):
    """A document carrying its own IR outlives the template that wrote it."""
    doc = _gen(client, "memo", fields=D.get_template("memo")["sample"]).json()
    stored = D._load_doc("default", doc["id"])
    stored["templateId"] = "retired-in-a-later-release"
    D._save_doc("default", stored)

    assert client.get(doc["download"]["md"]).status == 200
    assert client.get(f"/v1/documents/{doc['id']}").json()["kind"] == "doc"


def test_a_document_written_before_the_ir_still_renders(client):
    """Back-compat: {templateId, fields} and nothing else is asked of its template."""
    doc = _gen(client, "memo", fields=D.get_template("memo")["sample"]).json()
    stored = D._load_doc("default", doc["id"])
    stored.pop("ir"); stored.pop("kind")
    D._save_doc("default", stored)

    assert client.get(doc["download"]["md"]).status == 200
    assert client.get(f"/v1/documents/{doc['id']}").json()["kind"] == "doc"


def test_an_unrenderable_ir_is_refused_and_stores_nothing(client):
    before = len(client.get("/v1/documents/library").json()["documents"])
    assert client.post("/v1/documents", json={"kind": "sonnet", "ir": {}}).status == 400
    assert client.post("/v1/documents", json={"kind": "doc", "ir": "not an object"}).status == 400
    r = client.post("/v1/documents", json={"kind": "sheet", "ir": {"title": "x", "sheets": 7}})
    assert r.status == 422, r.text()
    assert len(client.get("/v1/documents/library").json()["documents"]) == before


def test_authored_documents_stay_in_their_own_org(client):
    ir = R.doc_ir("Private", [R.para("acme only")])
    doc = client.post("/v1/documents", json={"kind": "doc", "ir": ir},
                      headers={"X-Test-Org": "acme"}).json()
    assert client.get(f"/v1/documents/{doc['id']}").status == 404
    assert client.get(f"/v1/documents/{doc['id']}",
                      headers={"X-Test-Org": "acme"}).status == 200


# ── the frame archetype: a Frames composition ──────────────────────────────────────
# The other three archetypes hold content a renderer here draws. This one holds content
# that is ALREADY the artifact — a page that plays, which is the deliverable for web and
# for ads, and which `frames render` reads to make video. So the tests ask what studio
# can honestly assert about it, and never re-ask what `frames lint` asks.

# The canonical minimal composition (hanzoai/frames, skills/hyperframes-core references).
_COMPOSITION = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <title>Minimal Frames Composition</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0"
         data-width="1920" data-height="1080" data-duration="5">
      <section id="title-card" class="clip" data-start="0" data-duration="5"
               data-track-index="1"><h1 id="title">Hello Frames</h1></section>
    </div>
    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      tl.from("#title", { y: 48, opacity: 0, duration: 0.6, ease: "power3.out" }, 0.2);
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""


def test_a_composition_is_stored_and_served_exactly_as_authored(client):
    r = client.post("/v1/documents",
                    json={"kind": "frame", "ir": R.frame_ir("Hello Frames", _COMPOSITION)})
    assert r.status == 200, r.text()
    doc = r.json()
    assert doc["kind"] == "frame" and doc["formats"] == ["html"]
    assert doc["title"] == "Hello Frames"

    page = client.get(doc["download"]["html"])
    assert page.status == 200
    # Byte-for-byte: studio keeps the source, it does not rewrite it.
    assert page.read().decode() == _COMPOSITION


def test_a_composition_is_served_as_a_page_in_an_opaque_origin(client):
    """Watching it IS the web/ad artifact, so it is served inline — and authored markup
    must never be able to read this session or call these endpoints as the signed-in
    person, so it lands in a sandboxed origin with no same-origin grant."""
    doc = client.post("/v1/documents",
                      json={"kind": "frame", "ir": R.frame_ir("Ad", _COMPOSITION)}).json()
    page = client.get(doc["download"]["html"])
    assert page.headers["Content-Type"] == "text/html; charset=utf-8"
    assert page.headers["Content-Disposition"].startswith("inline;")
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    csp = page.headers["Content-Security-Policy"]
    assert "sandbox" in csp and "allow-same-origin" not in csp

    # Every other format stays a file.
    other = _gen(client, "memo", fields=D.get_template("memo")["sample"]).json()
    assert client.get(other["download"]["md"]).headers["Content-Disposition"].startswith("attachment;")


def test_html_that_is_not_a_composition_is_refused_and_stores_nothing(client):
    before = len(client.get("/v1/documents/library").json()["documents"])
    for html in ("<html><body><h1>a page, not a composition</h1></body></html>", "", "   "):
        r = client.post("/v1/documents", json={"kind": "frame", "ir": {"html": html}})
        assert r.status == 422, f"{html!r} -> {r.status}"
    # A root that states a length must state a real one.
    bad = _COMPOSITION.replace('data-duration="5"', 'data-duration="0"')
    assert client.post("/v1/documents", json={"kind": "frame", "ir": {"html": bad}}).status == 422
    assert len(client.get("/v1/documents/library").json()["documents"]) == before


def test_a_composition_may_leave_its_length_to_the_timeline(client):
    """`data-duration` is optional when the timeline implies it, so refusing a root
    without one would be stricter than frames — and would reject valid work."""
    html = _COMPOSITION.replace(' data-duration="5"', "", 1)
    r = client.post("/v1/documents", json={"kind": "frame", "ir": {"html": html}})
    assert r.status == 200, r.text()
    assert R.composition(html)["duration"] is None


def test_the_declared_shape_is_read_from_the_root(client):
    shape = R.composition(_COMPOSITION)
    assert shape == {"id": "main", "width": 1920.0, "height": 1080.0, "duration": 5.0}
    with pytest.raises(ValueError):
        R.composition("<html><body>no root</body></html>")
    with pytest.raises(ValueError):
        R.composition(_COMPOSITION.replace('data-width="1920"', 'data-width="wide"'))


def test_editing_the_composition_changes_the_page(client):
    doc = client.post("/v1/documents",
                      json={"kind": "frame", "ir": R.frame_ir("Ad", _COMPOSITION)}).json()
    ir = client.get(f"/v1/documents/{doc['id']}").json()["ir"]
    ir["html"] = ir["html"].replace("Hello Frames</h1>", "Ship it</h1>")

    r = client.patch(f"/v1/documents/{doc['id']}", json={"ir": ir})
    assert r.status == 200, r.text()
    assert "Ship it</h1>" in client.get(doc["download"]["html"]).read().decode()

    # A broken edit is refused and the stored page is untouched.
    assert client.patch(f"/v1/documents/{doc['id']}",
                        json={"ir": {"html": "<html>gone</html>"}}).status == 422
    assert "Ship it</h1>" in client.get(doc["download"]["html"]).read().decode()


def test_the_grammar_names_only_attributes_a_composition_really_carries(client):
    """The grammar exists so an author is accurate instead of guessing, which is worth
    nothing if it names an attribute the runtime never reads — an agent would write it
    and get a page that does not play."""
    g = R.GRAMMAR["frame"]
    for section in ("root", "clip"):
        for name in g[section]:
            if name.startswith("data-"):
                assert f'{name}="' in _COMPOSITION, f"grammar names {name}, no composition uses it"
    assert 'class="clip"' in _COMPOSITION
    assert "window.__timelines" in g["motion"] and "window.__timelines" in _COMPOSITION

    arch = client.get("/v1/documents/schema").json()["archetypes"]
    assert arch["frame"]["formats"] == ["html"]


def test_the_library_plays_a_composition_instead_of_downloading_it():
    """Served-HTML invariants: a page is opened, not saved to disk, and the click on a
    document with no template no longer claims a failure that did not happen."""
    home = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()
    assert "▶ Play" in home and 'target="_blank" rel="noopener"' in home
    assert "doc.kind==='frame'" in home            # the card click plays a composition
    assert "toast('Could not open document')" not in home


def test_a_composition_files_itself_under_its_own_shelf(client):
    doc = client.post("/v1/documents",
                      json={"kind": "frame", "ir": R.frame_ir("Ad", _COMPOSITION)}).json()
    entry = [d for d in client.get("/v1/documents/library").json()["documents"]
             if d["id"] == doc["id"]][0]
    assert entry["category"] == "Compositions"
    assert set(D.SHELF) == set(R.KIND_FORMATS), "every archetype needs a shelf"


# ── A hero film is a template, and its output is a composition ─────────────────
#
# The film generator (middleware/film.py) is a template's builder and nothing more:
# there is one store, one surface and one authoring model for a composition,
# whether a person wrote its HTML or the generator did. What the generator adds is
# that the rules a hero film has to keep are written rather than remembered — those
# are held in middleware_test/film_test.py, beside the generator.

from middleware import film  # noqa: E402

_FILM_FIELDS = {
    "src": "Custody Hero", "shape": "wide",
    "alt": "The credential is verified live and never returned.",
    "lead": "The credential your code never sees",
    "beats": [{"head": "Verified live", "note": "before anything is stored"},
              {"head": "Sealed", "note": "in your org's own KMS namespace"}],
    "foot": "api.hanzo.ai/v1/cloud",
}


def test_the_film_template_is_shelved_with_compositions():
    t = D.get_template("film")
    assert t["category"] == "Compositions" and t["formats"] == ["html"]
    assert "Compositions" in D.CATEGORY_ORDER
    assert any(g["name"] == "Compositions" for g in D.catalog_grouped())


def test_a_film_builds_a_composition_titled_for_its_files():
    """The document's TITLE is the stem the renderer names its master from, so the
    six files fall out of the pair rather than needing a third thing to hold them
    together — and a prefix somebody typed with spaces still names a file."""
    kind, ir = D.build_ir(D.get_template("film"), _FILM_FIELDS)
    assert kind == "frame"
    assert ir["title"] == "custody-hero-wide"
    assert R.composition(ir["html"])["width"] == 1920


def test_the_other_shape_is_the_same_words():
    tall = D.build_ir(D.get_template("film"), {**_FILM_FIELDS, "shape": "tall"})[1]
    wide = D.build_ir(D.get_template("film"), _FILM_FIELDS)[1]
    assert tall["title"] == "custody-hero-tall" and wide["title"] == "custody-hero-wide"
    assert R.composition(tall["html"])["width"] == 1080
    for beat in _FILM_FIELDS["beats"]:
        assert beat["head"] in tall["html"] and beat["head"] in wide["html"]


def test_a_shape_nobody_ships_falls_back_rather_than_raising():
    """Every builder in this catalog degrades — `prove` is what refuses content
    nothing can draw, and a template that raises makes that unreachable."""
    ir = D.build_ir(D.get_template("film"), {**_FILM_FIELDS, "shape": "square"})[1]
    assert ir["title"].endswith("-wide")


def test_authoring_a_film_stores_a_composition(client):
    r = client.post("/v1/documents/generate",
                    json={"templateId": "film", "fields": _FILM_FIELDS})
    assert r.status == 200, r.json()
    doc = r.json()
    assert doc["category"] == "Compositions" and doc["formats"] == ["html"]
    assert doc["title"] == "custody-hero-wide"
    # A composition is a page, and watching it IS the artifact — so what comes back
    # from the store is the composition itself, unchanged.
    played = client.get(f"/v1/documents/{doc['id']}/download?format=html")
    assert played.status == 200
    assert played.headers["Content-Disposition"].startswith("inline")
    assert R.composition(played.text())["id"] == "main"


def test_render_lands_the_files_through_the_one_ingest(tmp_path, monkeypatch):
    """Studio's library has ONE door, and a rendered master goes through it like
    every other render — so it is a gallery asset, not a private pile."""
    def outd(org=None):
        d = tmp_path / "orgs" / (org or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(folder_paths, "get_org_output_directory", outd)
    monkeypatch.setattr(film, "render", lambda html, work, stem: {
        "files": {n: tmp_path / n for n in
                  (f"{stem}.mp4", f"{stem}-first.jpg", f"{stem}-last.jpg")},
        "measured": {"width": 1920, "height": 1080, "seconds": 5.2}})

    seen = []
    app = web.Application()
    routes = web.RouteTableDef()
    D.add_documents_routes(routes, object(), org_of=lambda r: "default",
                           land=lambda org, rel, path: seen.append(rel))
    app.add_routes(routes)
    loop = asyncio.new_event_loop()
    tc = TestClient(TestServer(app), loop=loop)
    loop.run_until_complete(tc.start_server())
    try:
        c = _SyncClient(tc, loop)
        doc = c.post("/v1/documents/generate",
                     json={"templateId": "film", "fields": _FILM_FIELDS}).json()
        r = c.post(f"/v1/documents/{doc['id']}/render")
        assert r.status == 200, r.json()
        out = r.json()
        assert seen == ["film/custody-hero-wide.mp4", "film/custody-hero-wide-first.jpg",
                        "film/custody-hero-wide-last.jpg"]
        assert out["rendered"] == seen
        assert out["measured"]["width"] == 1920
        # Half a pair still knows the whole contract, because the stem names the shape.
        assert out["src"] == "custody-hero"
        assert out["names"] == film.names("custody-hero")
        assert out["frame"] == film.call_site("custody-hero", _FILM_FIELDS["alt"])
    finally:
        loop.run_until_complete(tc.close())
        loop.close()


def test_only_a_composition_renders_to_video(client):
    doc = client.post("/v1/documents/generate",
                      json={"templateId": "memo", "fields": {"subject": "hi"}}).json()
    r = client.post(f"/v1/documents/{doc['id']}/render")
    assert r.status == 400 and "composition" in r.json()["error"]


def test_render_without_the_toolchain_refuses_and_names_it(client, monkeypatch):
    """This image carries neither Node nor Chrome. A render asked for here says so,
    rather than queueing against a service that does not exist."""
    monkeypatch.setattr(film, "tools",
                        lambda: {"npx": "", "node": "", "ffmpeg": "/f", "ffprobe": "/p"})
    doc = client.post("/v1/documents/generate",
                      json={"templateId": "film", "fields": _FILM_FIELDS}).json()
    r = client.post(f"/v1/documents/{doc['id']}/render")
    assert r.status == 503
    body = r.json()
    assert "npx" in body["error"] and "node" in body["error"]
    assert body["tools"]["ffmpeg"] == "/f"


def test_one_org_never_renders_another_orgs_document(client):
    doc = client.post("/v1/documents/generate", json={"templateId": "film",
                      "fields": _FILM_FIELDS}, headers={"X-Test-Org": "a"}).json()
    r = client.post(f"/v1/documents/{doc['id']}/render", headers={"X-Test-Org": "b"})
    assert r.status == 404
