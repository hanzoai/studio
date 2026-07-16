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


def test_record_stores_node(orgdirs):
    r = worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                       output_prefix="f", pid="p1", lane="gpu", node="spark")
    assert r["node"] == "spark"
    r2 = worklog.record("acme", kind="fix", prompt="y", refs=[], uploads=[], parents=[],
                        output_prefix="g", pid="p2", lane="local")
    assert r2["node"] == "studio pod"   # local jobs run on the pod


def test_mark_started_finished_and_median(orgdirs):
    worklog.record("acme", kind="fix", prompt="x", refs=[], uploads=[], parents=[],
                   output_prefix="fixes/x", pid="p1")
    assert worklog.mark_started("acme", "p1", 1000)
    r = worklog.load("acme")[0]
    assert r["started_at"] == 1000 and r["status"] == "running"
    assert not worklog.mark_started("acme", "p1", 2000)          # idempotent: keeps first
    assert worklog.load("acme")[0]["started_at"] == 1000
    assert worklog.mark_finished("acme", "p1", 1050)
    r = worklog.load("acme")[0]
    assert r["finished_at"] == 1050 and r["status"] == "done"
    assert worklog.median_durations(worklog.load("acme"))["fix"] == 50   # 1050-1000


def test_median_durations_per_kind():
    rows = [
        {"kind": "fix", "ts": 100, "started_at": 100, "finished_at": 110},     # 10
        {"kind": "fix", "ts": 200, "started_at": 200, "finished_at": 220},     # 20
        {"kind": "fix", "ts": 300, "started_at": 300, "finished_at": 360},     # 60 → median 20
        {"kind": "compose", "ts": 0, "started_at": 0, "finished_at": 100},     # 100
        {"kind": "fix", "ts": 400},                                            # unfinished → ignored
    ]
    m = worklog.median_durations(rows)
    assert m == {"fix": 20, "compose": 100}
    # no started_at → duration falls back to finished − queued(ts)
    assert worklog.median_durations([{"kind": "rerun", "ts": 0, "finished_at": 30}]) == {"rerun": 30}


def test_estimate_lanes_positions_and_eta():
    now = 1000
    active = [
        {"id": "a", "kind": "fix", "lane": "gpu", "ts": 100, "status": "running", "started_at": now - 60, "node": "spark"},
        {"id": "b", "kind": "fix", "lane": "gpu", "ts": 200, "status": "queued", "node": "spark"},
        {"id": "c", "kind": "compose", "lane": "gpu", "ts": 300, "status": "queued", "node": "spark"},
        {"id": "d", "kind": "fix", "lane": "local", "ts": 150, "status": "queued", "node": "studio pod"},
    ]
    medians = {"fix": 120, "compose": 300}
    est, counts = worklog.estimate_lanes(active, medians, now)
    assert counts == {"gpu": 3, "local": 1}
    # running: elapsed 60 of median 120 → 60 remaining
    assert est["a"]["position"] == 1 and est["a"]["of"] == 3
    assert est["a"]["elapsed_seconds"] == 60 and est["a"]["remaining_seconds"] == 60 and est["a"]["eta_seconds"] == 60
    assert est["a"]["node"] == "spark"
    # queued #2: wait = running remaining(60); eta = 60 + own fix median(120)
    assert est["b"]["position"] == 2 and est["b"]["wait_seconds"] == 60 and est["b"]["eta_seconds"] == 180
    # queued #3: wait = 60 + b's fix(120) = 180; eta = 180 + compose(300)
    assert est["c"]["position"] == 3 and est["c"]["wait_seconds"] == 180 and est["c"]["eta_seconds"] == 480
    # separate lane, single queued item, nothing running → starts ~now
    assert est["d"]["position"] == 1 and est["d"]["of"] == 1 and est["d"]["wait_seconds"] == 0 and est["d"]["eta_seconds"] == 120


def test_estimate_lanes_default_median_for_unknown_kind():
    est, _ = worklog.estimate_lanes(
        [{"id": "x", "kind": "video", "lane": "gpu", "ts": 1, "status": "queued"}], {}, 0, default_median=200)
    assert est["x"]["eta_seconds"] == 200


def test_build_lineage_terminates_on_cycle():
    rows = [
        {"kind": "fix", "prompt": "x", "ts": 1, "parents": ["y.png"], "output": "x.png"},
        {"kind": "fix", "prompt": "y", "ts": 2, "parents": ["x.png"], "output": "y.png"},
    ]
    lin = worklog.build_lineage(rows, "x.png")   # must not loop forever
    assert lin["node"] == "x.png"
