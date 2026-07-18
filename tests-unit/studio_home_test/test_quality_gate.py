"""Unit tests for the render quality gate (middleware/quality_gate.py)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import quality_gate as qg
from middleware import studio_home as sh
from middleware import worklog


def test_parse_verdict_strict():
    assert qg.parse_verdict("PASS") == "pass"
    assert qg.parse_verdict("pass") == "pass"
    assert qg.parse_verdict("FAIL") == "fail"
    assert qg.parse_verdict("fail.") == "fail"
    assert qg.parse_verdict("FAIL — mottled, speckled skin") == "fail"
    assert qg.parse_verdict("The skin is clean. PASS") == "pass"
    # garbage / ambiguous / empty → inconclusive (the gate fails open on these)
    assert qg.parse_verdict("") == "inconclusive"
    assert qg.parse_verdict(None) == "inconclusive"
    assert qg.parse_verdict("MAYBE") == "inconclusive"
    assert qg.parse_verdict("PASS FAIL") == "fail"                  # both tokens → the LAST is the verdict (reasoning judges deliberate before concluding)
    assert qg.parse_verdict("failure to load") == "inconclusive"    # not a whole-token FAIL


def test_enabled_off_switch(monkeypatch):
    for off in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv("STUDIO_GATE", off)
        assert qg.enabled() is False
    for on in ("", "on", "1", "true", "yes"):
        monkeypatch.setenv("STUDIO_GATE", on)
        assert qg.enabled() is True
    monkeypatch.delenv("STUDIO_GATE", raising=False)
    assert qg.enabled() is True                                     # default ON


def test_config_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("STUDIO_GATE_MODEL", raising=False)
    monkeypatch.delenv("STUDIO_GATE_URL", raising=False)
    assert qg._model() == "zen-vl"                                  # the cloud's VL model
    assert qg._url().endswith("/v1/chat/completions")
    monkeypatch.setenv("STUDIO_GATE_URL", "http://127.0.0.1:1234/v1/chat/completions")
    assert qg._url() == "http://127.0.0.1:1234/v1/chat/completions"  # local-engine override
    monkeypatch.setenv("STUDIO_GATE_MODEL", "zen-vl-local")
    assert qg._model() == "zen-vl-local"


def _const(v):
    async def j(image_b64, bearer):
        return v
    return j


def _seed(tmp_path, status="draft", org="acme"):
    from PIL import Image
    root = tmp_path / "orgs" / org / "output"
    (root / "renders").mkdir(parents=True)
    Image.new("RGB", (8, 8), (200, 150, 120)).save(root / "renders" / "r.png", "PNG")
    (root / "library.json").write_text(json.dumps(
        {"assets": [{"path": "renders/r.png", "status": status}]}))
    return root


def _status(root, rel="renders/r.png"):
    lib = json.loads((root / "library.json").read_text())
    return next(a["status"] for a in lib["assets"] if a["path"] == rel)


@pytest.mark.asyncio
async def test_run_flags_on_fail_and_annotates_worklog(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    worklog.record("acme", kind="render", prompt="", refs=[], uploads=[], parents=[],
                   output_prefix="renders/r.png", node="spark", status="done")
    seen = {}

    async def fake_judge(image_b64, bearer):
        seen["b64"], seen["bearer"] = image_b64, bearer
        return "FAIL - mottled, speckled skin all over the arms and shoulders here"

    monkeypatch.setattr(qg, "judge", fake_judge)
    await qg.run("acme", root, "renders/r.png", "Bearer t")

    assert seen["b64"] and seen["bearer"] == "Bearer t"            # downscaled + same bearer
    assert _status(root) == "flagged"                             # asset hidden from default grid
    row = next(r for r in worklog.load("acme") if r.get("output_prefix") == "renders/r.png")
    ev = [e for e in row.get("events", []) if e.get("s") == "flagged"]
    assert len(ev) == 1 and ev[0]["note"].startswith("quality gate: FAIL")
    assert len(ev[0]["note"]) <= len("quality gate: ") + 80        # first 80 chars of the reply
    assert row["status"] == "done"                                # worklog lifecycle untouched — annotation only


@pytest.mark.asyncio
async def test_run_fails_open_on_pass_inconclusive_and_error(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    for verdict in ("PASS", "who knows", None):                   # pass · inconclusive · judge error (None)
        monkeypatch.setattr(qg, "judge", _const(verdict))
        await qg.run("acme", root, "renders/r.png", "Bearer t")
        assert _status(root) == "draft"

    async def boom(image_b64, bearer):
        raise RuntimeError("429 rate limited")                    # 402/429/timeout/unreachable class

    monkeypatch.setattr(qg, "judge", boom)
    await qg.run("acme", root, "renders/r.png", "Bearer t")       # must not raise
    assert _status(root) == "draft"                               # good work is never hidden by accident


@pytest.mark.asyncio
async def test_run_never_clobbers_curated_status(tmp_path, monkeypatch):
    root = _seed(tmp_path, status="approved")                    # a human already approved it
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    monkeypatch.setattr(qg, "judge", _const("FAIL"))
    await qg.run("acme", root, "renders/r.png", "Bearer t")
    assert _status(root) == "approved"                           # a late FAIL must not yank curated work


@pytest.mark.asyncio
async def test_run_missing_or_unreadable_is_a_no_op(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory", lambda o=None: str(root))
    called = {"n": 0}

    async def counting_judge(image_b64, bearer):
        called["n"] += 1
        return "FAIL"

    monkeypatch.setattr(qg, "judge", counting_judge)
    await qg.run("acme", root, "renders/gone.png", "Bearer t")   # no such file
    assert called["n"] == 0 and _status(root) == "draft"         # never judged, never flagged


@pytest.mark.asyncio
async def test_run_is_tenant_scoped(tmp_path, monkeypatch):
    a_root = _seed(tmp_path, org="acme")
    b_root = _seed(tmp_path, org="globex")
    roots = {"acme": a_root, "globex": b_root}
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory",
                        lambda o=None: str(roots.get(o, a_root)))
    monkeypatch.setattr(qg, "judge", _const("FAIL"))
    await qg.run("acme", a_root, "renders/r.png", "Bearer t")
    assert _status(a_root) == "flagged"
    assert _status(b_root) == "draft"                            # the other tenant is untouched


def test_schedule_off_switch_never_fires(monkeypatch):
    """STUDIO_GATE=off short-circuits before touching the loop — nothing scheduled."""
    monkeypatch.setenv("STUDIO_GATE", "off")
    fired = {"n": 0}
    monkeypatch.setattr(qg.asyncio, "ensure_future", lambda coro: fired.__setitem__("n", fired["n"] + 1))
    qg.schedule(object(), "acme", Path("/tmp"), "renders/r.png")
    assert fired["n"] == 0
