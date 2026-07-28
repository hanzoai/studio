"""Tests for /v1/design — the design system read and edited on disk.

A tmp checkout stands in for hanzoai/{brand,design,logo}, written in the shape the
real files have: prose comments, a declaration that only appears in a comment, no
space after the colon, and a token overridden again under ``.light``. Every edit is
asserted on the bytes left behind, and the routes run against a real aiohttp app so
the status codes are the real thing.
"""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from middleware import design as d

_COLORS = """\
/* Hanzo is monochrome. Was --background:#111111; before the OLED pass. */
:root{
  --neutral-50:#FAFAFA;
  --background: #000000;
}
.light{
  --background:#FFFFFF;
}
"""

_FONTS = ':root{ --font-sans:"Geist","Geist Sans",sans-serif; }\n'

_BRAND = {
    "brand": {
        "name": "Hanzo",
        "primaryColor": "#FFFFFF",
        "theme": {"dark": {"accent1": "#FFFFFF"}},
    },
    "chains": {"defaultChainId": 1},
}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A stand-in hanzoai checkout beside studio."""
    tokens = tmp_path / "design" / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "colors.css").write_text(_COLORS)
    (tokens / "fonts.css").write_text(_FONTS)
    marks = tmp_path / "logo" / "dist"
    marks.mkdir(parents=True)
    (marks / "logo.svg").write_text("<svg viewBox='0 0 67 67'/>")
    brand = tmp_path / "brand" / "brand.json"
    brand.parent.mkdir(parents=True)
    brand.write_text(json.dumps(_BRAND, indent=2) + "\n")
    monkeypatch.setattr(d, "_BRAND", brand)
    monkeypatch.setattr(d, "_TOKENS", tokens)
    monkeypatch.setattr(d, "_MARKS", marks)
    return tmp_path


def colors(checkout):
    return (checkout / "design" / "tokens" / "colors.css").read_text()


def identity(checkout):
    return json.loads((checkout / "brand" / "brand.json").read_text())["brand"]


# --- reading -------------------------------------------------------------

def test_read_flattens_the_identity(checkout):
    brand = d.read()["brand"]
    assert brand["name"] == "Hanzo"
    assert brand["theme.dark.accent1"] == "#FFFFFF"


def test_read_ignores_everything_but_the_identity(checkout):
    assert "defaultChainId" not in d.read()["brand"]


def test_read_returns_marks_as_svg(checkout):
    assert d.read()["marks"] == {"logo": "<svg viewBox='0 0 67 67'/>"}


def test_first_declaration_wins(checkout):
    """The dark :root default, not the .light override — the rule gen-tokens.mjs
    applies to build the package's TypeScript view."""
    assert d.read()["tokens"]["--background"] == "#000000"


def test_a_declaration_in_a_comment_is_not_a_token(checkout):
    assert d.read()["tokens"]["--background"] != "#111111"


def test_reads_every_token_file(checkout):
    tokens = d.read()["tokens"]
    assert tokens["--neutral-50"] == "#FAFAFA"
    assert tokens["--font-sans"] == '"Geist","Geist Sans",sans-serif'


def test_absent_checkout_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_BRAND", tmp_path / "nope" / "brand.json")
    monkeypatch.setattr(d, "_TOKENS", tmp_path / "nope" / "tokens")
    monkeypatch.setattr(d, "_MARKS", tmp_path / "nope" / "dist")
    assert d.read() == {"brand": {}, "tokens": {}, "marks": {}}


# --- writing -------------------------------------------------------------

def test_edit_keeps_the_authored_line(checkout):
    d.write({"--neutral-50": "#F0F0F0"})
    assert "  --neutral-50:#F0F0F0;\n" in colors(checkout)


def test_edit_leaves_the_rest_of_the_file_alone(checkout):
    d.write({"--neutral-50": "#F0F0F0"})
    text = colors(checkout)
    assert text.startswith("/* Hanzo is monochrome.")
    assert "  --background: #000000;\n" in text


def test_edit_writes_only_the_first_declaration(checkout):
    d.write({"--background": "#0A0A0A"})
    text = colors(checkout)
    assert "  --background: #0A0A0A;\n" in text
    assert "  --background:#FFFFFF;\n" in text     # the .light override is untouched


def test_edit_reaches_a_nested_identity_value(checkout):
    d.write({"theme.dark.accent1": "#EDEDED"})
    assert identity(checkout)["theme"]["dark"]["accent1"] == "#EDEDED"


def test_edit_keeps_what_it_did_not_touch(checkout):
    """brand.json is rewritten canonically, so the guarantee is on the values, not
    on the formatting: nothing outside the edit is lost."""
    d.write({"primaryColor": "#EDEDED"})
    doc = json.loads((checkout / "brand" / "brand.json").read_text())
    assert doc["chains"] == {"defaultChainId": 1}
    assert doc["brand"]["name"] == "Hanzo"


def test_edit_spans_both_kinds_at_once(checkout):
    d.write({"name": "Hanzo AI", "--neutral-50": "#F0F0F0"})
    assert identity(checkout)["name"] == "Hanzo AI"
    assert d.read()["tokens"]["--neutral-50"] == "#F0F0F0"


def test_a_value_survives_a_round_trip(checkout):
    d.write({"--font-sans": '"Basel Grotesk",sans-serif'})
    assert d.read()["tokens"]["--font-sans"] == '"Basel Grotesk",sans-serif'


# --- the routes ----------------------------------------------------------

def _app():
    app = web.Application()
    routes = web.RouteTableDef()
    d.add_design_routes(routes, object())
    app.add_routes(routes)
    return app


def _run(coro):
    return asyncio.run(coro)


def _patch(body):
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.patch("/v1/design", json=body)
            return r.status, await r.json()
    return _run(go())


def test_get_serves_the_design_system(checkout):
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.get("/v1/design")
            return r.status, await r.json()
    status, body = _run(go())
    assert status == 200
    assert body["brand"]["name"] == "Hanzo"
    assert body["tokens"]["--background"] == "#000000"
    assert "logo" in body["marks"]


def test_patch_persists_and_answers_with_the_new_system(checkout):
    status, body = _patch({"--background": "#0A0A0A"})
    assert status == 200
    assert body["tokens"]["--background"] == "#0A0A0A"
    assert "  --background: #0A0A0A;\n" in colors(checkout)


def test_patch_refuses_an_unknown_name(checkout):
    status, body = _patch({"--nope": "#000000", "name": "Hanzo AI"})
    assert status == 400
    assert body["names"] == ["--nope"]
    assert identity(checkout)["name"] == "Hanzo"     # nothing was written


def test_patch_refuses_a_mark(checkout):
    status, body = _patch({"logo": "<svg/>"})
    assert status == 400
    assert body["names"] == ["logo"]


def test_patch_refuses_a_body_that_is_not_a_map(checkout):
    assert _patch(["--background", "#000000"])[0] == 400


def test_patch_refuses_a_value_that_smuggles_a_second_declaration(checkout):
    """`[^;\\n]+` bounds what a write REPLACES, never what it inserts. A value
    carrying `;` used to append a whole extra declaration: the read reported the
    value that was sent while the cascade resolved the smuggled one."""
    status, body = _patch({"--background": "#000; --neutral-50:#FF0000; x:y"})
    assert status == 400
    assert body["names"] == ["--background"]
    css = colors(checkout)
    assert "#FF0000" not in css
    assert "x:y" not in css
    assert "  --background: #000000;\n" in css     # untouched


def test_patch_refuses_a_value_that_re_cuts_the_file(checkout):
    before = colors(checkout)
    status, _ = _patch({"--background": "#000;\n}\n:root{ --neutral-50:#FF0000;"})
    assert status == 400
    assert colors(checkout) == before
    assert before.count("{") == 2 and before.count("}") == 2


def test_patch_refuses_a_list_of_pairs(checkout):
    """dict() happily builds a mapping out of one, which made two request shapes
    for one operation."""
    assert _patch([["--background", "#ABCDEF"]])[0] == 400
    assert "#ABCDEF" not in colors(checkout)


def test_patch_is_refused_off_loopback(checkout, monkeypatch):
    """A write lands in two other git repos on this machine. Reading is open;
    editing is not a tenant operation, and there is only one design system."""
    monkeypatch.setattr(d, "is_loopback_request", lambda request: False)
    status, body = _patch({"--background": "#0A0A0A"})
    assert status == 403
    assert "  --background: #000000;\n" in colors(checkout)


def test_get_is_open_off_loopback(checkout, monkeypatch):
    monkeypatch.setattr(d, "is_loopback_request", lambda request: False)

    async def go():
        app = web.Application()
        routes = web.RouteTableDef()
        d.add_design_routes(routes, None)
        app.add_routes(routes)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/v1/design")
            return r.status

    assert _run(go()) == 200
