"""Guard tests for the browser-injected publishable key (middleware/studio_home.py).

The publishable key is the ONE credential Studio prints into page source, so these
pin the invariants that keep a server secret out of the client:

  * ``_publishable`` is an ALLOWLIST -- only ``pk-`` (the gateway's single
    client-safe prefix, iamauth.go) survives; every server-secret family
    (``hk-``/``sk-``/``fw_``/``hz_``) and JWT is dropped (fail CLOSED).
  * ``_hz_config_script`` cannot be broken out of its ``<script>`` tag, even by a
    hostile value that also satisfies the allowlist.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import studio_home as sh

_PREFIX, _SUFFIX = "<script>window.HZ=", ";</script>"
_LINE_SEP, _PARA_SEP = chr(0x2028), chr(0x2029)   # raw JS statement terminators (U+2028/9)


def _parse_hz(script: str) -> dict:
    assert script.startswith(_PREFIX) and script.endswith(_SUFFIX)
    payload = script[len(_PREFIX):-len(_SUFFIX)]
    return json.loads(payload.replace("<\\/", "</"))  # reverse the </ neutralization


@pytest.mark.parametrize("secret", [
    "hk-live-abc", "sk-live-abc", "fw_live_abc", "hz_live_abc",   # gateway server-secret families
    "eyJhbGciOiJIUzI1NiJ9.body.sig",                              # a JWT
    "secret-abc", "PK-upper", "pk_underscore", "pkno-dash",       # not the pk- publishable prefix
    "   ", "",                                                     # blank
])
def test_publishable_drops_everything_but_pk(secret):
    assert sh._publishable(secret) == ""


@pytest.mark.parametrize("pk,want", [
    ("pk-abc123", "pk-abc123"),
    ("  pk-abc123  ", "pk-abc123"),   # trimmed
])
def test_publishable_passes_pk(pk, want):
    assert sh._publishable(pk) == want


@pytest.mark.parametrize("env", ["PUBLISHABLE_KEY"])
def test_hz_config_injects_publishable_env(monkeypatch, env):
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(env, "pk-live-xyz")
    assert _parse_hz(sh._hz_config_script())["pk"] == "pk-live-xyz"


def test_publishable_key_is_the_only_spelling():
    # PUBLISHABLE_KEY is the fleet's ONE name (KMS deploy/PUBLISHABLE_KEY). No
    # fallbacks: a second accepted name is a second place to look when the page
    # ships keyless, and lets a chart satisfy a name the code does not read.
    assert sh._PUBLISHABLE_KEY_ENV == ("PUBLISHABLE_KEY",)


def test_hz_config_ignores_the_retired_names(monkeypatch):
    # The old spellings are not consulted. Setting them alone leaves the page
    # keyless, which is what makes a stale chart LOUD instead of silently fine.
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    for retired in ("EVENT_INGEST_KEY", "NEXT_PUBLIC_HANZO_PUBLISHABLE_KEY",
                    "STUDIO_PUBLISHABLE_KEY"):
        monkeypatch.setenv(retired, "pk-live-retired")
    assert _parse_hz(sh._hz_config_script())["pk"] == ""


def test_hz_config_drops_a_non_publishable_value(monkeypatch):
    # A server secret in the one slot is dropped by the allowlist and nothing
    # replaces it -- fail closed, never hand the browser a non-pk- value.
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLISHABLE_KEY", "hk-live-server-secret")
    assert _parse_hz(sh._hz_config_script())["pk"] == ""


def test_hz_config_drops_secret_env(monkeypatch):
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLISHABLE_KEY", "hz_server_secret")   # a real server key
    assert _parse_hz(sh._hz_config_script())["pk"] == ""


def test_hz_config_empty_when_unset(monkeypatch):
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    assert _parse_hz(sh._hz_config_script())["pk"] == ""


def test_hz_config_script_cannot_break_out(monkeypatch):
    # A hostile value that still satisfies the pk- allowlist must not be able to
    # close the <script> tag or smuggle a raw JS line/paragraph separator (U+2028/9).
    for k in sh._PUBLISHABLE_KEY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLISHABLE_KEY", "pk-</script><img>" + _LINE_SEP + _PARA_SEP)
    script = sh._hz_config_script()
    body = script[len(_PREFIX):-len(_SUFFIX)]
    assert "</" not in body                                # no tag can be opened/closed
    assert _LINE_SEP not in body and _PARA_SEP not in body  # separators escaped, not raw
    assert _parse_hz(script)["pk"].startswith("pk-")       # value survives intact, just neutralized
