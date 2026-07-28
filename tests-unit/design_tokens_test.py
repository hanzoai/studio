"""The sign-in page's design tokens must actually resolve.

An undefined CSS custom property paints NOTHING and reports no error: no console
message, no failed request, no build warning. That silence is why this class of
bug survives review, so it needs a test rather than an eye.

studio.hanzo.ai serves ``web/login.html``, which paints entirely from
``@hanzo/design`` tokens carried in ``web/marketing/shell.css``. That bundle is a
COMMITTED build artifact, so a rebuild that drops the token import would ship a
blank-looking page and nothing else in the suite would notice. These tests are
that notice.

Reachability, not mere presence, is what is asserted. ``@hanzo/design`` 0.3.0's
``typography.css`` references ``var(--font-display)`` with no fallback while
defining it in ``fonts.css`` — a file this bundle cannot import, because it
self-hosts Geist via ``url()`` and esbuild has no ``.woff2`` loader here. That
reference is only reachable through ``--type-hero/-h2/-h3``, which nothing on
this surface uses, so it cannot affect a painted pixel. Walking the graph from
real declarations records that distinction instead of hiding it behind an
allowlist.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOGIN = ROOT / "web" / "login.html"
SHELL_CSS = ROOT / "web" / "marketing" / "shell.css"
SHELL_JS = ROOT / "web" / "marketing" / "shell.js"

# One page load: the stylesheet, the module and the document that pull together.
GROUP = (LOGIN, SHELL_CSS, SHELL_JS)

DEF = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;}]*)")
VAR = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(,)?")


def _read(paths):
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in paths}


def _definitions(sources):
    """name -> declared value, across every file the page loads."""
    out = {}
    for text in sources.values():
        for m in DEF.finditer(text):
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def _painting_refs(sources, defs):
    """var() references that sit in a real declaration, not inside a token value.

    A token referenced only from another token's value paints nothing until some
    real property reaches it, so the two cases are not equivalent.
    """
    refs = {}
    for text in sources.values():
        spans = [m.span(2) for m in DEF.finditer(text)]
        for m in VAR.finditer(text):
            inside = any(a <= m.start() < b for a, b in spans)
            if inside:
                continue
            name, has_fallback = m.group(1), bool(m.group(2))
            refs[name] = has_fallback if name not in refs else (refs[name] and has_fallback)
    return refs


def _reachable(refs, defs):
    """Close the reference set over the values of the tokens it reaches."""
    seen, stack, out = set(), list(refs), dict(refs)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for m in VAR.finditer(defs.get(name, "")):
            child, has_fallback = m.group(1), bool(m.group(2))
            out[child] = has_fallback if child not in out else (out[child] and has_fallback)
            stack.append(child)
    return out


@pytest.fixture(scope="module")
def page():
    for p in GROUP:
        assert p.exists(), f"{p} is missing; the committed shell bundle is part of the page"
    sources = _read(GROUP)
    defs = _definitions(sources)
    return sources, defs, _reachable(_painting_refs(sources, defs), defs)


def test_every_reachable_token_resolves(page):
    """No token the page can actually paint with may be undefined."""
    _, defs, reachable = page
    silent = sorted(n for n, has_fallback in reachable.items() if n not in defs and not has_fallback)
    assert not silent, (
        "these custom properties are referenced with no definition and no literal "
        f"fallback, so they paint nothing and say nothing: {silent}"
    )


def test_unresolved_tokens_still_carry_a_literal_fallback(page):
    """What the page does not define, it must fall back for.

    ``--hanzo-accent`` is the live example: @hanzo/design deliberately declines to
    invent a Hanzo-specific accent inside a package that Lux and Zoo fork, so the
    shell supplies ``var(--hanzo-accent, #ffffff)`` and stays correct either way.
    """
    _, defs, reachable = page
    for name, has_fallback in sorted(reachable.items()):
        if name not in defs:
            assert has_fallback, f"{name} is undefined and has no fallback"


def test_shell_bundle_carries_the_design_tokens(page):
    """A rebuild that drops the @hanzo/design import must fail here, not in prod.

    These four are the ones ``@hanzogui/shell`` reads. Without them it silently
    falls back to a Tailwind scale (12/14/16/18/20/24px) that renders studio's
    chrome 1-3px larger than every surface loading @hanzo/design or @hanzo/brand
    1.4.4, which both say 11/13/14/15/17/21px.
    """
    _, defs, _ = page
    for name in ("--font-size-xs", "--font-size-base", "--z-dropdown", "--z-popover"):
        assert name in defs, f"{name} absent from web/marketing/shell.css — rebuild web/shell"


def test_focus_indicator_is_a_design_token(page):
    """The sign-in page must paint a real focus ring, not the UA default.

    It had none and fell through to the browser's own outline, which measured
    1.70:1 against the white button. ``--ring`` is #737373 as of @hanzo/design
    0.3.0, which clears the 3:1 WCAG 2.4.11 wants.
    """
    sources, _, _ = page
    login = sources[LOGIN]
    assert ":focus-visible" in login, "no focus indicator on the sign-in page"
    assert "var(--ring" in login, "focus indicator does not come from the token layer"


def test_login_declares_no_palette_of_its_own(page):
    """The page must not grow a private vocabulary back.

    It used to hand-copy eleven console.hanzo.ai hex values into a local :root.
    Seven were blue-tinted while the comment claimed monochrome, and those names
    shadowed @hanzo/design's own --border/--muted/--primary/--radius.
    """
    sources, _, _ = page
    body = re.sub(r"/\*.*?\*/", "", sources[LOGIN], flags=re.S)
    declared = sorted(set(DEF.findall(body)) and {m.group(1) for m in DEF.finditer(body)})
    assert not declared, f"web/login.html declares its own tokens again: {declared}"
