"""Unit tests for the per-org work log (middleware/worklog.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from middleware import worklog


@pytest.fixture
def orgdirs(tmp_path, monkeypatch):
    """Map each org to its own output dir, exactly as folder_paths does per-org, so
    the tests exercise real cross-tenant isolation (one file per org)."""
    def out_dir(org=None):
        d = tmp_path / "orgs" / (org or "default") / "output"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    monkeypatch.setattr(worklog.folder_paths, "get_org_output_directory", out_dir)
    return out_dir


def test_record_and_load_roundtrip(orgdirs):
    row = worklog.record("acme", kind="fix", prompt="brighten", refs=["r.png"],
                         uploads=[], parents=["a.png"], output_prefix="fixes/a",
                         pid="pid-1", lane="gpu")
    assert row["id"] == "pid-1" and row["status"] == "queued" and row["lane"] == "gpu"
    rows = worklog.load("acme")
    assert len(rows) == 1 and rows[0]["parents"] == ["a.png"] and rows[0]["kind"] == "fix"


def test_failed_dispatch_still_recorded_with_reason(orgdirs):
    row = worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[],
                         parents=["a.png"], output_prefix="fixes/a",
                         status="failed", error="no GPU worker online for this org")
    assert row["status"] == "failed" and row["id"].startswith("failed-")
    assert "no GPU worker" in row["error"]
    assert any(e["s"] == "failed" for e in row["events"])   # visible, not swallowed


def test_set_status_advances_but_respects_terminal(orgdirs):
    worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                   output_prefix="fixes/a", pid="p1")
    assert worklog.set_status("acme", "p1", "running")
    assert worklog.load("acme")[0]["status"] == "running"
    assert worklog.set_status("acme", "p1", "cancelled")
    # a terminal row is frozen — a late 'done' can't resurrect a cancelled job
    assert not worklog.set_status("acme", "p1", "done")
    assert worklog.load("acme")[0]["status"] == "cancelled"


def test_cross_tenant_isolation(orgdirs):
    worklog.record("acme", kind="fix", prompt="secret acme prompt", refs=[], uploads=[],
                   parents=["a.png"], output_prefix="fixes/a", pid="p1")
    # a different org's log is a different file — zero leakage either direction
    assert worklog.load("acme") and worklog.load("globex") == []
    worklog.record("globex", kind="fix", prompt="globex prompt", refs=[], uploads=[],
                   parents=["b.png"], output_prefix="fixes/b", pid="p2")
    assert [r["prompt"] for r in worklog.load("acme")] == ["secret acme prompt"]
    assert [r["prompt"] for r in worklog.load("globex")] == ["globex prompt"]


def test_cap_keeps_newest(orgdirs, monkeypatch):
    monkeypatch.setattr(worklog, "_CAP", 5)
    for i in range(12):
        worklog.record("acme", kind="fix", prompt=f"p{i}", refs=[], uploads=[],
                       parents=[], output_prefix=f"fixes/{i}", pid=f"p{i}")
    rows = worklog.load("acme")
    assert len(rows) == 5 and [r["prompt"] for r in rows] == [f"p{i}" for i in range(7, 12)]


def test_build_lineage_chain():
    # a.png --fix--> b.png --fix--> c.png ; and a.png --fix--> d.png (a sibling branch)
    rows = [
        {"kind": "fix", "prompt": "step1", "ts": 1, "parents": ["a.png"], "output": "b.png"},
        {"kind": "fix", "prompt": "step2", "ts": 2, "parents": ["b.png"], "output": "c.png"},
        {"kind": "fix", "prompt": "branch", "ts": 3, "parents": ["a.png"], "output": "d.png"},
    ]
    lin = worklog.build_lineage(rows, "c.png")
    assert [a["path"] for a in lin["ancestors"]] == ["a.png", "b.png"]   # oldest first
    assert lin["node"] == "c.png"
    assert lin["descendants"] == []
    # from the root, descendants include both branches, chronological
    lin2 = worklog.build_lineage(rows, "a.png")
    assert [d["path"] for d in lin2["descendants"]] == ["b.png", "d.png", "c.png"]
    assert lin2["ancestors"] == []


def test_build_lineage_terminates_on_cycle():
    rows = [
        {"kind": "fix", "prompt": "x", "ts": 1, "parents": ["y.png"], "output": "x.png"},
        {"kind": "fix", "prompt": "y", "ts": 2, "parents": ["x.png"], "output": "y.png"},
    ]
    lin = worklog.build_lineage(rows, "x.png")   # must not loop forever
    assert lin["node"] == "x.png"
