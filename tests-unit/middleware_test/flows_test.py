"""The per-KIND flow policy + queue-honesty contracts.

- FLOWS frames REPLACE the edit-preservation clause (a CAD drawing converts to a
  photograph instead of staying a drawing) and extend the negative.
- An unknown/absent kind never shapes anything — the user's words ride unshaped.
- A flow's denoise is the lane default; an explicit strength still wins.
- Engine phases that mean "expired" read as failed, never "queued".
- A transport-failed dispatch HOLDS the job (visible, re-enqueueable, cancellable)
  instead of losing the user's prompt.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sh = importlib.import_module("middleware.studio_home")


def test_flow_of_known_and_unknown():
    assert sh._flow_of("cad_ghost") is sh.FLOWS["cad_ghost"]
    assert sh._flow_of("CAD_GHOST") is sh.FLOWS["cad_ghost"]   # case-insensitive
    assert sh._flow_of("music") is None                        # non-render card: inert
    assert sh._flow_of("") is None and sh._flow_of(None) is None


def test_flows_are_well_formed():
    for kind, f in sh.FLOWS.items():
        assert f["frame"] and f["negative"], kind
        assert sh.EDIT_DENOISE_MIN <= f["denoise"] <= sh.EDIT_DENOISE_MAX, kind
        # No flow frame may smuggle the preservation clause back in.
        assert "keep the same subject" not in f["frame"], kind


def test_cad_flow_replaces_preservation_clause_and_extends_negative():
    flow = sh.FLOWS["cad_ghost"]
    g = sh._fix_graph("cad.png", "make it the red colorway", "fixes/cad",
                      flow=flow, denoise=flow["denoise"])
    pos, neg = g["6"]["inputs"]["prompt"], g["7"]["inputs"]["prompt"]
    assert "keep the same subject" not in pos          # the sketch-preserver is gone
    assert "PHOTOREALISTIC" in pos and "annotation marks" in pos
    assert pos.startswith("make it the red colorway")  # the user's words lead
    assert "pen strokes" in neg and neg.startswith(sh._NEG)
    assert g["13"]["inputs"]["denoise"] == flow["denoise"]


def test_no_flow_keeps_default_edit_contract():
    g = sh._fix_graph("x.png", "fix the strap", "fixes/x")
    assert "keep the same subject" in g["6"]["inputs"]["prompt"]
    assert g["13"]["inputs"]["denoise"] == sh.EDIT_DENOISE_DEFAULT


def test_edit_denoise_flow_default_and_explicit_strength():
    flow = sh.FLOWS["cad_mockup"]
    assert sh._edit_denoise(None, default=flow["denoise"]) == flow["denoise"]
    assert sh._edit_denoise(0.3, default=flow["denoise"]) == 0.3   # caller wins
    assert sh._edit_denoise(None) == sh.EDIT_DENOISE_DEFAULT


def test_image_graph_frames_positive_for_flow():
    g = sh._image_graph("red bikini front view", "image/x", flow=sh.FLOWS["product"])
    pos = g["5"]["inputs"]["text"]
    assert pos.startswith("red bikini front view")
    assert "e-commerce product photograph" in pos
    g2 = sh._image_graph("red bikini front view", "image/x")
    assert g2["5"]["inputs"]["text"] == "red bikini front view"


def test_compose_graph_flow_frame():
    g = sh._compose_graph(["a.png", "b.png"], "swap the fabric", "composes/x",
                          flow=sh.FLOWS["cad_mockup"])
    pos = g["6"]["inputs"]["prompt"]
    assert "Combine the provided reference images" in pos
    assert "annotation marks" in pos
    assert "flat illustration" in g["7"]["inputs"]["prompt"]


def test_expired_phases_read_failed():
    for phase in ("TIMED_OUT", "TIMED_OUT_SCHEDULE_TO_START", "EXPIRED"):
        assert sh._PHASE[phase] == "failed"
    assert sh._PHASE["SCHEDULED"] == "queued"


def test_hold_job_spools_and_cancels(tmp_path, monkeypatch):
    import middleware.worklog as wl
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    common = dict(kind="fix", prompt="make it red", refs=["a.png"], uploads=[],
                  parents=["p.png"], output_prefix="fixes/x", params={})
    pid = sh._hold_job("acme", {"1": {"class_type": "SaveImage", "inputs": {}}}, common)
    assert pid.startswith("held-")
    held = sh._list_held("acme")
    assert len(held) == 1 and held[0]["prompt"] == "make it red" and held[0]["graph"]
    # The worklog row is QUEUED — never lost, never failed.
    row = next(r for r in wl.load("acme") if r.get("id") == pid)
    assert row["status"] == "queued" and row["node"] == "waiting"
    # Tenancy: another org sees nothing.
    assert sh._list_held("rival") == []
    # Cancel deletes the spool so the job can never re-enqueue afterwards.
    sh._cancel_job(type("S", (), {"prompt_queue": None})(), "acme", pid)
    assert sh._list_held("acme") == []
    assert next(r for r in wl.load("acme") if r.get("id") == pid)["status"] == "cancelled"


def test_holdable_matches_transport_errors_only():
    holdable = ["GPU render worker is momentarily unavailable — please retry.",
                "render could not be queued — no GPU worker online for this org",
                "could not reach the render engine (boom)",
                "render was not queued (no prompt_id returned)"]
    for msg in holdable:
        assert any(s in msg.lower() for s in sh._HOLDABLE), msg
    for msg in ["instruction required (3-500 chars)", "unknown asset: x.png",
                "select 2-5 references to compose"]:
        assert not any(s in msg.lower() for s in sh._HOLDABLE), msg
