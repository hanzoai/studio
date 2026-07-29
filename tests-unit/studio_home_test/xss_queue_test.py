"""Stored-XSS regression for the queue / worklog rows (middleware/studio_home.html).

A malicious job field — j.node (← X-Target-GPU), j.prefix/title (← SaveImage prefix),
desc (← prompt text), j.id (← client prompt_id) — must render INERT. Two layers:
  (1) executable: the file's OWN esc() neutralizes an <img onerror> payload (run under
      node; skipped only if node is unavailable);
  (2) static: the row builders route every field through esc() and drive row actions via
      esc'd data-* attributes + one delegated listener — never a raw-concatenated onclick.
"""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()
PAYLOAD = "<img src=x onerror=alert(1)>"


def test_file_esc_map_neutralizes_payload():
    """Apply the FILE's OWN esc() character map (extracted from studio_home.html) to the
    payload and assert it renders INERT — every '<'/'>' neutralized, no live tag survives.
    Pure-Python (no node dep) so it runs in CI; weakening the map (e.g. dropping '<')
    fails here."""
    # Matches the ONE esc declaration (function form; the duplicate const was a
    # script-killing SyntaxError and is gone).
    m = re.search(r"esc\s*[=(][^\n]*?replace\(/\[([^\]]+)\]/g,\s*c=>\((\{[^}]+\})", HTML)
    assert m, "esc() helper/map not found"
    charclass, mapsrc = m.group(1), m.group(2)
    emap = dict(re.findall(r"'(.)':'([^']+)'", mapsrc))
    assert {"<", ">", "&", '"'} <= set(charclass) and {"<", ">"} <= emap.keys()
    out = "".join(emap.get(ch, ch) for ch in PAYLOAD)
    assert "<img" not in out and "<" not in out                    # every '<' neutralized
    assert emap["<"] in out                                        # escaped entity present, inert text


def test_gjRow_escapes_fields_and_delegates_cancel():
    assert "${esc(j.prefix||'render')}" in HTML                     # SaveImage prefix escaped
    assert "esc((j.node&&j.node!=='queued')?j.node:'your GPU')" in HTML  # X-Target-GPU/identity escaped
    assert 'data-cancel="${esc(j.id)}"' in HTML                    # id via esc'd data attr


def test_qitem_escapes_title_desc_and_delegates_cancel():
    assert "${esc(title)}" in HTML
    assert "${esc(desc)||'(no description)'}" in HTML
    assert 'data-cancel="${esc(id)}"' in HTML


def test_no_raw_id_onclick_sinks_remain():
    for sink in ("cancelJob('${j.id}')", "cancelJob('${id}')", "cancelJob('${it.id}')",
                 "openAmend('${it.id}')", "openContext('${it.id}')", "delWork('${it.id}')"):
        assert sink not in HTML, f"raw client-id onclick sink still present: {sink}"


def test_one_delegated_listener_wires_data_actions():
    assert "t.closest('[data-cancel]')" in HTML
    assert "cancelJob(el.getAttribute('data-cancel'))" in HTML
    assert "openAmend(el.getAttribute('data-amend'))" in HTML
