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
        if "denoise" in f:
            assert sh.EDIT_DENOISE_MIN <= f["denoise"] <= sh.EDIT_DENOISE_MAX, kind
        # No flow frame may smuggle the preservation clause back in.
        assert "keep the same subject" not in f["frame"], kind


def test_only_conversion_lanes_license_a_re_render():
    """denoise follows the TRANSFORMATION DISTANCE, not the output style.

    `product` shipped at 0.90 and an ordinary "remove the drawn lines" edit came back
    with the watercolor print replaced by plaid and a different model — a style lane
    must never license a re-render. Only drawing→photograph conversions may.
    """
    conversions = {"cad_ghost", "cad_mockup"}
    for kind, f in sh.FLOWS.items():
        d = f.get("denoise", sh.EDIT_DENOISE_DEFAULT)
        if kind in conversions:
            assert d >= 0.85, f"{kind} must re-render enough to become a photograph (got {d})"
        else:
            assert d <= 0.5, (f"{kind} only names a LOOK — at {d} it re-renders the image and "
                              "destroys the print, the model and the resolution")
    # A style lane inherits the preserving default, so an edit stays an edit.
    for kind in ("product", "lifestyle"):
        g = sh._fix_graph("a.png", "remove the drawn lines", "fixes/a",
                          flow=sh.FLOWS[kind],
                          denoise=sh._edit_denoise(None, default=sh.FLOWS[kind].get(
                              "denoise", sh.EDIT_DENOISE_DEFAULT)))
        assert g["13"]["inputs"]["denoise"] == sh.EDIT_DENOISE_DEFAULT, kind


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


def test_activity_phase_reads_long_form_engine_statuses():
    # The engine emits ACTIVITY_TASK_STATE_* enum names; a terminal status must
    # NEVER read as queued (the phantom-queue bug: 400+ finished jobs as depth).
    for raw, want in (("ACTIVITY_TASK_STATE_CANCELED", "cancelled"),
                      ("ACTIVITY_TASK_STATE_COMPLETED", "done"),
                      ("ACTIVITY_TASK_STATE_FAILED", "failed"),
                      ("ACTIVITY_TASK_STATE_SCHEDULED", "queued"),
                      ("ACTIVITY_TASK_STATE_STARTED", "running"),
                      ("SCHEDULED", "queued"), ("COMPLETED", "done")):
        assert sh._activity_phase({"status": raw}) == want, raw
    assert sh._activity_phase({"status": "SOMETHING_NEW"}) == "queued"  # unknown stays optimistic


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


def test_one_unqueueable_held_job_never_strands_the_rest(tmp_path, monkeypatch):
    """A single poisoned spool entry must not hold the queue hostage.

    _retry_held used to `return` on ANY DispatchError, so the first un-requeueable job
    blocked every other held job forever — observed in prod as the same 5 ids across
    every queue read, none ever moving. A TRANSPORT error still stops the sweep (the
    rest would fail identically); anything else fails that ONE job, with its reason.
    """
    import asyncio
    import middleware.worklog as wl
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    common = dict(prompt="p", refs=[], uploads=[], parents=[], params={})
    bad = sh._hold_job("acme", {"1": {"class_type": "SaveImage", "inputs": {}}},
                       dict(kind="fix", output_prefix="fixes/bad", **common))
    good = sh._hold_job("acme", {"2": {"class_type": "SaveImage", "inputs": {}}},
                        dict(kind="fix", output_prefix="fixes/good", **common))
    assert len(sh._list_held("acme")) == 2

    order = sorted([bad, good])            # _list_held order decides who is "first"
    async def fake_queue(request, server, graph):
        # The FIRST entry is permanently un-requeueable; the second is fine.
        if graph.get(list(graph)[0]) and order[0] == (bad if "1" in graph else good):
            raise sh.DispatchError("unknown asset: gone.png")
        return "new-pid"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    monkeypatch.setattr(sh, "_in_render_jobs", lambda org, pid: False)

    asyncio.run(sh._retry_held(None, None, "acme"))
    # BOTH left the spool: one re-queued, one failed with its reason — neither stranded.
    assert sh._list_held("acme") == []
    rows = {r["id"]: r for r in wl.load("acme")}
    assert rows[order[0]]["status"] == "failed"
    assert "could not re-queue" in str(rows[order[0]].get("events") or rows[order[0]])

    # A TRANSPORT failure still stops the sweep and KEEPS the spool (work is not lost).
    held = sh._hold_job("acme", {"3": {"class_type": "SaveImage", "inputs": {}}},
                        dict(kind="fix", output_prefix="fixes/t", **common))
    async def down(request, server, graph):
        raise sh.DispatchError("GPU render worker is momentarily unavailable — please retry.")
    monkeypatch.setattr(sh, "_queue_prompt", down)
    asyncio.run(sh._retry_held(None, None, "acme"))
    assert [r["id"] for r in sh._list_held("acme")] == [held]


def test_a_job_wearing_transport_words_does_not_pin_the_others(tmp_path, monkeypatch):
    """A holdable MESSAGE is not proof of a holdable CONDITION.

    "momentarily unavailable" is what the dispatcher says for ANY enqueue failure —
    it names the fleet without checking — so a job that simply cannot be enqueued
    (a 4K source whose inlined body exceeds the API's request-size limit) wears
    transport words and re-holds forever. The spool is walked in sorted order, so the
    alphabetically-first of those pinned all ten of one org's renders across two days
    while both GPUs sat idle and healthy.

    Trying the rest costs nothing when the fabric really is down (they all fail and the
    spool is intact either way) and is the whole difference when it is not.
    """
    import asyncio
    import middleware.worklog as wl
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    common = dict(prompt="p", refs=[], uploads=[], parents=[], params={})
    big = sh._hold_job("acme", {"BIG": {"class_type": "SaveImage", "inputs": {}}},
                       dict(kind="fix", output_prefix="fixes/big", **common))
    ok = sh._hold_job("acme", {"OK": {"class_type": "SaveImage", "inputs": {}}},
                      dict(kind="fix", output_prefix="fixes/ok", **common))

    async def fake_queue(request, server, graph):
        if "BIG" in graph:      # too large to enqueue, reported as if the fleet were down
            raise sh.DispatchError("GPU render worker is momentarily unavailable — please retry.")
        return "new-pid"
    monkeypatch.setattr(sh, "_queue_prompt", fake_queue)
    monkeypatch.setattr(sh, "_in_render_jobs", lambda org, pid: False)

    asyncio.run(sh._retry_held(None, None, "acme"))
    left = [r["id"] for r in sh._list_held("acme")]
    assert ok not in left, "a queueable job was stranded behind one that was not"
    assert left == [big], "the un-enqueueable job is still HELD (its work is not lost)"


def test_holdable_matches_transport_errors_only():
    # "was not queued" was here and is NOT transport: the engine returns it for a graph
    # it REFUSED (missing model, stale staged input), which repeats forever. Since
    # _retry_held stops the sweep on a holdable error, one such job pinned the whole
    # spool — ten jobs sat unmoved for hours while fresh dispatches succeeded beside
    # them. A job the engine will never accept must FAIL with its reason.
    assert not any(s in "render was not queued (no prompt_id returned)".lower()
                   for s in sh._HOLDABLE), "a refusal must not be held as transport"
    holdable = ["GPU render worker is momentarily unavailable — please retry.",
                "render could not be queued — no GPU worker online for this org",
                "could not reach the render engine (boom)",
                "commerce: no available server",
                "dial unix /var/lib/cloud/run/commerce.sock: connection refused"]
    for msg in holdable:
        assert any(s in msg.lower() for s in sh._HOLDABLE), msg
    for msg in ["instruction required (3-500 chars)", "unknown asset: x.png",
                "select 2-5 references to compose"]:
        assert not any(s in msg.lower() for s in sh._HOLDABLE), msg


def test_positive_encoders_by_sampler_wiring():
    g = {"5": {"class_type": "CLIPTextEncode", "inputs": {"text": "user words"}},
         "6": {"class_type": "CLIPTextEncode", "inputs": {"text": sh._NEG + ", extra lesson"}},
         "13": {"class_type": "KSampler", "inputs": {"positive": ["5", 0], "negative": ["6", 0]}}}
    assert sh._positive_encoders(g) == {"5"}


def test_mcp_tools_carry_truth_contract_and_new_tools():
    from middleware import mcp
    names = {t["name"] for t in mcp._TOOLS}
    assert {"fix", "compose", "rerun", "status", "ask"} <= names
    for t in mcp._TOOLS:
        if t["name"] in ("fix", "compose", "rerun"):
            assert "NEVER" in t["description"] and "QUEUED" in t["description"], t["name"]
    q = mcp._queued({"ok": True, "prompt_id": "x"})
    assert q["state"] == "queued" and "QUEUED" in q["note"]


def test_global_negative_bans_construction_lines():
    # The permanent house rule: CAD/pen construction marks never render, any lane.
    for term in ("pen markings", "construction lines", "annotation marks",
                 "drawn black lines on fabric"):
        assert term in sh._NEG, term


# ── Output integrity: done means 100% complete ───────────────────────────────

_PNG_OK = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PNG_CUT = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # no IEND: a truncated write


def test_intact_reads_format_terminal_markers(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(_PNG_OK)
    assert sh._intact(good) is True
    bad = tmp_path / "b.png"
    bad.write_bytes(_PNG_CUT)
    assert sh._intact(bad) is False
    # MP4: a consistent top-level box chain is completion evidence…
    mp4 = tmp_path / "c.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x10ftyp" + b"\x00" * 8 + b"\x00\x00\x00\x08free")
    assert sh._intact(mp4) is True
    # …a chain that overruns EOF is truncation.
    cut = tmp_path / "d.mp4"
    cut.write_bytes(b"\x00\x00\x00\x10ftyp" + b"\x00" * 8 + b"\x00\x00\x00\x20mdat")
    assert sh._intact(cut) is False
    # Unknown format: no evidence either way.
    other = tmp_path / "e.webm"
    other.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert sh._intact(other) is None


def test_truncated_output_never_reads_done(tmp_path, monkeypatch):
    import os
    import time
    calls = {}
    monkeypatch.setattr(sh.worklog, "set_status",
                        lambda org, id_, st, note=None: calls.setdefault(id_, (st, note)))
    monkeypatch.setattr(sh.worklog, "mark_finished",
                        lambda org, id_, mt: calls.setdefault(id_, ("done", mt)))
    (tmp_path / "fixes").mkdir()
    (tmp_path / "fixes" / "a_00001_.png").write_bytes(_PNG_OK)
    (tmp_path / "fixes" / "b_00001_.png").write_bytes(_PNG_CUT)
    (tmp_path / "fixes" / "c_00001_.png").write_bytes(_PNG_CUT)
    old = time.time() - 120
    for name in ("a_00001_.png", "b_00001_.png"):
        os.utime(tmp_path / "fixes" / name, (old, old))          # settled
    now = int(time.time())
    rows = [{"id": i, "status": "queued", "output_prefix": f"fixes/{i}",
             "ts": int(old) - 5, "params": {}} for i in ("a", "b", "c")]
    sh._finalize_landed("acme", tmp_path, rows, now)
    assert rows[0]["status"] == "done"                            # complete: done
    assert rows[1]["status"] == "failed"                          # truncated: failed
    assert "truncated" in calls["b"][1]
    assert rows[2]["status"] == "queued"                          # fresh: still judged later


def test_fold_engine_terminal_verdicts_reach_the_worklog(monkeypatch):
    # An engine-failed job leaves no landed output, so only this fold can ever
    # close its worklog row — without it the row reads queued FOREVER.
    calls = {}
    monkeypatch.setattr(sh.worklog, "set_status",
                        lambda org, id_, st, note=None: calls.setdefault(id_, (st, note)))
    acts = [
        {"execution": {"runId": "f1"}, "status": "ACTIVITY_TASK_STATE_FAILED",
         "failureCause": "studio.render: /v1/worker/execute HTTP 400: bad model"},
        {"execution": {"runId": "c1"}, "status": "ACTIVITY_TASK_STATE_CANCELED"},
        {"execution": {"runId": "r1"}, "status": "ACTIVITY_TASK_STATE_STARTED"},
        {"execution": {"runId": "d1"}, "status": "ACTIVITY_TASK_STATE_COMPLETED"},
    ]
    rows = [{"id": i, "status": "queued"} for i in ("f1", "c1", "r1", "d1", "x1")]
    sh._fold_engine("acme", rows, acts)
    st = {r["id"]: r["status"] for r in rows}
    assert st["f1"] == "failed" and "bad model" in calls["f1"][1]
    assert st["c1"] == "cancelled"
    # A worker CLAIMED and STARTED r1 — that is a fact, so it must not read "queued"
    # while it samples on a GPU (the same lie as a failed job reading queued).
    assert st["r1"] == "running" and calls["r1"][0] == "running"
    assert st["d1"] == "queued"        # done is the integrity gate's verdict, never folded
    assert st["x1"] == "queued"        # not in the engine: untouched
    sh._fold_engine("acme", rows, None)  # engine unreachable: no-op, never raises


def test_an_explicit_conversion_request_actually_converts():
    """The mirror of the plaid failure: "make this into a CAD drawing" at the 0.25 edit
    default returned the source VISIBLY UNCHANGED. Too little change is as wrong as too
    much, so an EXPLICIT rewrite request raises the distance — and an ordinary edit
    never does."""
    for asks in ("make this into a CAD drawing", "turn this into a real life version",
                 "convert to a photograph", "show it as a photorealistic version",
                 "redraw this as a technical flat"):
        assert sh.asks_conversion(asks), asks
    for edit in ("remove the drawn black lines from the top", "brighten slightly",
                 "make the bottom a thong cut", "remove any visible stitching",
                 "the suit is supposed to be like a corset so it sucks in her waist"):
        assert not sh.asks_conversion(edit), edit
    # …and the distance actually reaches the graph, with an explicit strength still winning.
    g = sh._fix_graph("cad.png", "make this into a CAD drawing", "fixes/c",
                      denoise=sh._edit_denoise(None, default=sh.CONVERSION_DENOISE))
    assert g["13"]["inputs"]["denoise"] == sh.CONVERSION_DENOISE >= 0.85
    assert sh._edit_denoise(0.3, default=sh.CONVERSION_DENOISE) == 0.3


def test_heartbeat_window_covers_a_cold_load_plus_a_full_sample():
    """The heartbeat rides SAMPLING steps, so the window must also cover the cold model
    load before step 1. At 1200s six real renders were reclaimed as "lease expired:
    worker unresponsive" while the GPU was busy — the engine failing working jobs."""
    import inspect
    import re
    from middleware import gpu_dispatch
    src = inspect.getsource(gpu_dispatch.dispatch_if_worker)
    m = re.search(r'"heartbeatTimeout":\s*"(\d+)s"', src)
    assert m, "heartbeatTimeout must be declared on the enqueue"
    assert int(m.group(1)) >= 3600, (
        f"{m.group(1)}s is too tight: a cold 20GB load plus a 30-step sample on a "
        "memory-thrashing host exceeds it, and a premature reclaim costs the render")


def test_op_of_reads_the_output_prefix_not_the_kind():
    # `kind` carries the FLOW when one was active (product/cad_ghost/…), so only the
    # recorded output prefix can name the operation to replay.
    assert sh._op_of({"output_prefix": "fixes/x_ab12", "kind": "cad_ghost"}) == "fix"
    assert sh._op_of({"output_prefix": "composes/y", "kind": "product"}) == "compose"
    assert sh._op_of({"output_prefix": "video/v", "kind": "video"}) == "video"
    assert sh._op_of({"output_prefix": "model3d/m"}) == "model3d"
    assert sh._op_of({"output_prefix": "image/i", "kind": "product"}) == "image"
    assert sh._op_of({"output_prefix": "renders/mystery"}) == ""
    assert sh._op_of({}) == ""


def test_retry_row_replays_through_the_one_funnel(monkeypatch):
    """A retry must call the SAME dispatch a fresh submit calls — that is how it picks up
    the current rules (flow frames, the construction-lines negative, the integrity gate)
    with no replay-only path — and must REFUSE rather than approximate an unrebuildable row."""
    import asyncio
    seen = {}

    async def fake_fix(request, server, org, rel, instruction, **kw):
        seen.update(op="fix", rel=rel, instruction=instruction, flow=kw.get("flow_kind"))
        return {"ok": True, "prompt_id": "n1"}

    async def fake_compose(request, server, org, paths, instruction, **kw):
        seen.update(op="compose", paths=paths, uploads=kw.get("uploads"), flow=kw.get("flow_kind"))
        return {"ok": True, "prompt_id": "n2"}

    monkeypatch.setattr(sh, "dispatch_fix", fake_fix)
    monkeypatch.setattr(sh, "dispatch_compose", fake_compose)

    # A failed FLOW fix replays with its flow intact and its own source.
    row = {"id": "f1", "status": "failed", "kind": "cad_ghost", "output_prefix": "fixes/a_b",
           "prompt": "remove the drawn black lines", "parents": ["designs/cad.png"], "uploads": []}
    out = asyncio.run(sh.retry_row(None, None, "acme", row))
    assert out["prompt_id"] == "n1"
    assert seen == {"op": "fix", "rel": "designs/cad.png",
                    "instruction": "remove the drawn black lines", "flow": "cad_ghost"}

    # A compose replays every recorded source, library paths and uploads alike.
    seen.clear()
    asyncio.run(sh.retry_row(None, None, "acme", {
        "id": "c1", "status": "failed", "kind": "compose", "output_prefix": "composes/x",
        "prompt": "merge", "parents": ["a.png"], "uploads": ["up_1.png"]}))
    assert seen["op"] == "compose" and seen["paths"] == ["a.png"] and seen["uploads"] == ["up_1.png"]

    # Refusals: no source for a fix, too few sources for a compose, unknown prefix.
    for bad, why in (
        ({"output_prefix": "fixes/a", "prompt": "x", "parents": []}, "source asset"),
        ({"output_prefix": "composes/a", "prompt": "x", "parents": ["only.png"]}, "2-5 recorded sources"),
        ({"output_prefix": "renders/a", "prompt": "x"}, "unrecognized job type"),
    ):
        try:
            asyncio.run(sh.retry_row(None, None, "acme", bad))
        except sh.DispatchError as e:
            assert why in str(e), (why, str(e))
        else:
            raise AssertionError(f"expected a refusal for {bad}")


def test_home_script_has_no_duplicate_declarations():
    # A second top-level const/let/class of an already-declared name is a
    # script-killing SyntaxError: the ENTIRE home app is discarded while shell.js
    # keeps the page LOOKING alive (the dead-tabs production incident).
    import collections
    import re
    html = (Path(__file__).resolve().parents[2] / "middleware" / "studio_home.html").read_text()
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    decls = re.findall(
        r"^(const|let|class)\s+([A-Za-z_$][\w$]*)|^(?:async\s+)?(function)\s+([A-Za-z_$][\w$]*)\s*\(",
        script, re.M)
    names = [(k or f, n or fn) for k, n, f, fn in decls]
    counts = collections.Counter(n for _, n in names)
    strict = {n for k, n in names if k in ("const", "let", "class")}
    dupes = sorted(n for n, c in counts.items() if c > 1 and n in strict)
    assert dupes == [], f"duplicate top-level declarations kill the whole script: {dupes}"


def test_index_upload_refuses_truncated_file(tmp_path, monkeypatch):
    import middleware.worklog as wl
    monkeypatch.setattr(sh.stacks_store, "absorb", lambda *a, **k: None)
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    root = tmp_path / "lib"
    (root / "up").mkdir(parents=True)
    (root / "up" / "partial.png").write_bytes(_PNG_CUT)
    assert sh._index_upload("acme", root, "up/partial.png",
                            design=None, kind="render", role=None, node="upload") is False
    assert sh._load_library(root).get("assets", []) == []         # never surfaced
    (root / "up" / "ok.png").write_bytes(_PNG_OK)
    assert sh._index_upload("acme", root, "up/ok.png",
                            design=None, kind="render", role=None, node="upload") is True


def test_compose_negative_carries_the_same_references_as_the_positive():
    """CFG subtracts the negative from the positive, so they must describe the same thing.

    TextEncodeQwenImageEditPlus attaches a reference LATENT per image — it does not
    only encode words. A negative built from the prompt alone is therefore a different
    structure, not merely different words, and every sampling step subtracted a term
    computed over latents the positive did not share. That is what returned the two
    source photographs ghosted over each other under a stipple. The single-image edit
    has always mirrored them; this pins the multi-input case to the same rule.
    """
    g = sh._compose_graph(["a.png", "b.png"], "put the suit on her", "composes/x")
    enc = [n for n in g.values() if n.get("class_type") == "TextEncodeQwenImageEditPlus"]
    assert len(enc) == 2
    pos, neg = (enc[0], enc[1]) if "worst quality" in str(enc[1]["inputs"]["prompt"]) else (enc[1], enc[0])
    refs = lambda n: {k: v for k, v in n["inputs"].items() if k.startswith("image") or k == "vae"}
    assert refs(neg) == refs(pos), "the negative must see every reference the positive does"
    assert neg["inputs"]["prompt"] != pos["inputs"]["prompt"], "only the WORDS may differ"


def test_compose_with_a_base_edits_it_instead_of_synthesising_a_blend():
    """"Put the garment from A onto the scene in B" is an EDIT of B, not a blend.

    Without a base the sampler starts from an EMPTY latent at denoise 1.0 — it
    synthesises a new image guided by every reference equally — and the real result was
    a ghosted double exposure of both photographs, because nothing said which one was
    being made. With a base, that image is the starting latent, so its pose and framing
    survive and the others are only references.
    """
    refs = ["scene.png", "outfit.png"]
    blend = sh._compose_graph(refs, "combine them", "composes/x")
    assert blend["10"]["class_type"] == "EmptySD3LatentImage"
    assert blend["13"]["inputs"]["denoise"] == 1.0          # synthesis, unchanged

    onto = sh._compose_graph(refs, "dress her in the two-piece", "composes/y",
                             base="scene.png", flow=sh.FLOWS["cad_mockup"])
    # The base is ENCODED as the latent — the same wiring an edit uses.
    assert onto["10"]["class_type"] == "VAEEncode"
    assert onto[onto["13"]["inputs"]["latent_image"][0]]["class_type"] == "VAEEncode"
    assert not any(n.get("class_type", "").startswith("Empty") for n in onto.values())
    # …and it leads the conditioning, with the other reference still present. Loaded
    # ONCE: the node feeding the latent is the very node image1 points at, so a base can
    # never displace a reference or be staged twice.
    pos = next(n for n in onto.values() if n.get("class_type") == "TextEncodeQwenImageEditPlus")
    assert pos["inputs"]["image1"] == onto["10"]["inputs"]["pixels"]
    assert "image2" in pos["inputs"]
    assert sum(1 for n in onto.values() if n.get("class_type") == "LoadImage") == len(refs)
    # A painting→photograph conversion travels far; an explicit strength still wins.
    assert onto["13"]["inputs"]["denoise"] == sh.FLOWS["cad_mockup"]["denoise"]
    exact = sh._compose_graph(refs, "swap the top", "composes/z", base="scene.png", denoise=0.4)
    assert exact["13"]["inputs"]["denoise"] == 0.4


def test_the_held_sweep_is_bounded_so_it_cannot_stall_the_server(tmp_path, monkeypatch):
    """One queue read may not make an unbounded number of enqueue attempts.

    The enqueue is a SYNCHRONOUS urlopen on the event loop. Trying every held job took
    studio down: ten entries, each shipping a ~25MB body that the far end reset, blocked
    every request in the process — the readiness probe included — so the pod went unready
    and the ingress served 503. Progress past a stuck entry is worth having; an
    unbounded stall is not.
    """
    import asyncio
    import middleware.worklog as wl
    monkeypatch.setattr(sh.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    monkeypatch.setattr(wl.folder_paths, "get_org_output_directory",
                        lambda org: str(tmp_path / "orgs" / org / "output"))
    common = dict(prompt="p", refs=[], uploads=[], parents=[], params={})
    for i in range(10):
        sh._hold_job("acme", {f"N{i}": {"class_type": "SaveImage", "inputs": {}}},
                     dict(kind="fix", output_prefix=f"fixes/h{i}", **common))

    attempts = []
    async def always_down(request, server, graph):
        attempts.append(1)
        raise sh.DispatchError("GPU render worker is momentarily unavailable — please retry.")
    monkeypatch.setattr(sh, "_queue_prompt", always_down)

    asyncio.run(sh._retry_held(None, None, "acme"))
    assert len(attempts) <= sh._HOLD_TRIES, (
        f"one read attempted {len(attempts)} enqueues; each is a blocking upload")
    assert len(sh._list_held("acme")) == 10, "nothing was lost — they stay held"


def test_an_edit_keeps_the_master_and_adds_only_what_changed():
    """Resizing the edit is not restoring the master.

    Measured on a real 4096x6144 render: high-frequency energy 10.3 in the camera
    original, 0.6 in the resized edit — 17.2MB of photograph arriving as 3.0MB of soft.
    Making the picture bigger recovered no fabric weave.

    So the deliverable keeps every photographed pixel and adds only the model's change:
    original + (edit_upscaled - original_at_the_model's_resolution). Both sides pass
    through the same bucket, so the bracket is exactly the adjustment that was asked
    for. Same render: detail 9.9 against the master's 10.3, brightness carried through.
    """
    g = sh._fix_graph("m.png", "brighten very slightly", "fixes/m",
                      size=(4096, 6144), denoise=0.25)
    save = g["15"]["inputs"]["images"][0]
    assert g[save]["class_type"] == "ImageAddMulti", "the master must be the base of the output"

    # The original — node 1, the full-resolution LoadImage — is what is added TO.
    assert g["20"]["inputs"]["image_1"] == ["1", 0], "the camera's own pixels are the base"
    # The change is measured against the original AS THE MODEL SAW IT (node 2, the
    # bucket), never against a resample — otherwise the difference includes our own
    # scaling and the master gets edited by arithmetic it never asked for.
    assert g["17"]["inputs"]["image"] == ["2", 0]
    # Signed, as two disjoint one-sided halves, because the node clamps at zero.
    assert g["18"]["inputs"]["blending"] == "subtract" and g["18"]["inputs"]["image_1"] == ["16", 0]
    assert g["19"]["inputs"]["blending"] == "subtract" and g["19"]["inputs"]["image_1"] == ["17", 0]
    assert g["20"]["inputs"]["blending"] == "add"
    assert g["21"]["inputs"]["blending"] == "subtract"
    # Full strength: a partial blend would apply a fraction of the edit the user asked for.
    for n in ("18", "19", "20", "21"):
        assert g[n]["inputs"]["blend_amount"] == 1.0, n
    assert not sh._assert_edit_graph(g)


def test_a_conversion_delivers_the_model_not_the_master():
    """At conversion strength the model's output IS the deliverable.

    denoise already says how far the image must travel. A drawing→photograph
    conversion (0.85+) is asked to REPLACE what was photographed, so compositing the
    original back over it would fight the conversion. Only the edit band — where the
    master is still the subject — keeps the master.
    """
    g = sh._fix_graph("m.png", "make it a photograph", "fixes/m",
                      size=(4096, 6144), denoise=sh.CONVERSION_DENOISE)
    save = g["15"]["inputs"]["images"][0]
    assert g[save]["class_type"] == "ImageScale", "a conversion delivers the model's own image"
    assert "21" not in g, "no composite at conversion strength"
    # A garment swap replaces a large region but keeps the same photograph, so it stays
    # on the master side of the line.
    swap = sh._fix_graph("m.png", "swap the top", "fixes/m", size=(4096, 6144), denoise=0.45)
    assert "21" in swap, "a swap still keeps the photograph it edits"


def test_without_a_known_size_the_graph_is_unchanged():
    """An unreadable source costs the master's size, never the render.

    Absent a size the graph is exactly what it was, so a source whose dimensions
    cannot be read still edits — it just returns the bucket, as before.
    """
    g = sh._fix_graph("m.png", "brighten", "fixes/m")
    assert g["15"]["inputs"]["images"][0] == "14", "saves the decode directly"
    assert "16" not in g, "no scaling node is added when there is nothing to restore to"
    assert not sh._assert_edit_graph(g)


def test_source_size_reads_real_dimensions_and_never_raises(tmp_path):
    """_source_size answers from the file, and answers None rather than failing."""
    from PIL import Image
    p = tmp_path / "s.png"
    Image.new("RGB", (1234, 567)).save(p)
    assert sh._source_size(p) == (1234, 567)
    assert sh._source_size(tmp_path / "missing.png") is None
    bad = tmp_path / "notimage.png"
    bad.write_text("not an image")
    assert sh._source_size(bad) is None
