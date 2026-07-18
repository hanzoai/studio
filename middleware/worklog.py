"""Per-org work log — the ONE record of every render this org dispatched.

The single dispatch funnel (``studio_home.dispatch_fix`` / ``dispatch_compose`` /
``rerun``) writes one row here at dispatch time, so the studio surface can show:

  * the queue/history with REAL status — queued · running · done · failed ·
    cancelled — including gpu-lane jobs the local /queue can't see (that was the
    'nothing showed as queued' report) and dispatches that failed before queuing;
  * the full context that produced each item — prompt, reference images/uploads,
    the workflow family (fix / compose / …), params, and the process trail;
  * parent → child lineage: each derived asset records the library asset(s) it was
    derived from, so any item's versions (ancestors + later fixes) form a chain.

Storage is one JSON array per org at ``orgs/<org>/output/worklog.json`` — the same
per-org metadata pattern as ``library.json`` / ``render_jobs.json`` / ``ratings.json``
(atomic tmp+replace). No new database. No backfill: history starts when this ships;
items generated earlier have no recorded row (and derived items no recorded parent).

Tenant isolation: every function takes an already-resolved ``org`` (the caller's
IAM org, resolved server-side by ``studio_home._org_of``). A row lives only in its
org's file, so one org can never read or mutate another's log.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import folder_paths

# Cap the file so a busy org can't grow it without bound; oldest rows drop off.
_CAP = 1000
_TERMINAL = ("done", "failed", "cancelled")


def _dir(org: str) -> Path:
    return Path(folder_paths.get_org_output_directory(org))


def _path(org: str) -> Path:
    return _dir(org) / "worklog.json"


def load(org: str) -> list[dict]:
    try:
        rows = json.loads(_path(org).read_text())
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save(org: str, rows: list[dict]) -> None:
    d = _dir(org)
    d.mkdir(parents=True, exist_ok=True)
    p = _path(org)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rows[-_CAP:]))
    tmp.replace(p)


def has_output(org: str, path: str) -> bool:
    """Whether any row already owns this landed file — the ingest upsert key
    (re-ingesting must repair, never duplicate). A dispatch row's output_prefix
    lacks the SaveImage counter suffix (``fixes/x_ab12cd`` owns
    ``fixes/x_ab12cd_00001_.png``), so the match is prefix-aware, not equality —
    equality alone doubled every mirrored render with an ingest row."""
    for r in load(org):
        pre = r.get("output_prefix") or ""
        if path and (path == pre or path == r.get("output") or
                     (pre and path.startswith(pre + "_"))):
            return True
    return False


def record(org: str, *, kind: str, prompt: str, refs: list, uploads: list,
           parents: list, output_prefix: str, params: dict | None = None,
           pid: str | None = None, lane: str | None = None, node: str | None = None,
           status: str = "queued", error: str | None = None) -> dict:
    """Append one dispatch row. ``pid`` is the /prompt id on success (used to match
    live queue/history); a failed dispatch has no id, so a synthetic one is minted
    and the row is marked failed with the reason so it STILL shows (never silent)."""
    now = int(time.time())
    row: dict = {
        "id": pid or f"failed-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "kind": kind,
        "prompt": (prompt or "")[:800],
        "refs": [str(x) for x in (refs or [])][:8],
        "uploads": [str(x) for x in (uploads or [])][:8],
        "parents": [str(x) for x in (parents or [])][:8],
        "output_prefix": output_prefix or "",
        "params": params or {},
        "lane": lane or ("failed" if status == "failed" else "local"),
        "node": node or ("studio pod" if (lane in (None, "local")) else "gpu"),
        "status": status,
        "updatedAt": now,
        "events": [{"s": "queued", "ts": now}],
    }
    if error:
        row["error"] = str(error)[:400]
        row["events"].append({"s": "failed", "ts": now, "note": row["error"]})
    rows = load(org)
    rows.append(row)
    _save(org, rows)
    return row


def set_status(org: str, pid: str, status: str, note: str | None = None) -> bool:
    """Advance a row's status by prompt_id (running/done/failed/cancelled). Returns
    True if a row was updated. Idempotent-safe; terminal rows are left untouched."""
    if not pid:
        return False
    rows = load(org)
    hit = False
    now = int(time.time())
    for r in rows:
        if r.get("id") == pid and r.get("status") not in _TERMINAL:
            r["status"] = status
            r["updatedAt"] = now
            r.setdefault("events", []).append(
                {"s": status, "ts": now, **({"note": note} if note else {})})
            hit = True
    if hit:
        _save(org, rows)
    return hit


def add_event(org: str, output_prefix: str, s: str, note: str | None = None) -> bool:
    """Append one event to the row(s) carrying this landed output — an annotation,
    not a lifecycle move. The render completed (its status is terminal); a later
    judgment like the quality gate flagging it records an event without touching
    that status. Keyed by output like ``has_output``. Returns True if a row was
    annotated."""
    if not output_prefix:
        return False
    rows = load(org)
    now = int(time.time())
    hit = False
    for r in rows:
        if r.get("output_prefix") == output_prefix or r.get("output") == output_prefix:
            r.setdefault("events", []).append(
                {"s": s, "ts": now, **({"note": note} if note else {})})
            r["updatedAt"] = now
            hit = True
    if hit:
        _save(org, rows)
    return hit


def mark_started(org: str, pid: str, at: int) -> bool:
    """Stamp when a job first begins rendering (first seen at the head of its lane).
    Idempotent: only the first observation sets started_at. Feeds duration = finished
    − started, the basis of the ETA median."""
    if not pid:
        return False
    rows = load(org)
    hit = False
    for r in rows:
        if r.get("id") == pid and not r.get("started_at") and r.get("status") not in _TERMINAL:
            r["started_at"] = int(at)
            r["status"] = "running"
            r["updatedAt"] = int(time.time())
            r.setdefault("events", []).append({"s": "running", "ts": int(at)})
            hit = True
    if hit:
        _save(org, rows)
    return hit


def mark_finished(org: str, pid: str, at: int) -> bool:
    """Stamp real completion (the output file's mtime) and close the row as done."""
    if not pid:
        return False
    rows = load(org)
    hit = False
    for r in rows:
        if r.get("id") == pid and r.get("status") not in _TERMINAL:
            r["finished_at"] = int(at)
            r["status"] = "done"
            r["updatedAt"] = int(time.time())
            r.setdefault("events", []).append({"s": "done", "ts": int(at)})
            hit = True
    if hit:
        _save(org, rows)
    return hit


def _duration(r: dict) -> float | None:
    """Measured render seconds for a completed row: finished − started when both
    exist (true render time), else finished − queued (turnaround, an upper bound)."""
    fin = r.get("finished_at")
    if fin is None:
        return None
    start = r.get("started_at")
    if start is None:
        start = r.get("ts")
    if start is None:
        return None
    return float(fin - start) if fin > start else None


def median_durations(rows: list[dict]) -> dict:
    """Rolling median render seconds per kind (fix/compose/rerun) over completed
    rows — the ETA basis. Pure. Only kinds with a measured sample appear."""
    buckets: dict[str, list[float]] = {}
    for r in rows:
        d = _duration(r)
        if d and d > 0:
            buckets.setdefault(r.get("kind", ""), []).append(d)
    out: dict[str, int] = {}
    for k, v in buckets.items():
        v.sort()
        n = len(v)
        out[k] = int(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)
    return out


def estimate_lanes(active: list[dict], medians: dict, now: int,
                   default_median: int = 180) -> tuple[dict, dict]:
    """Pure position + ETA math, per lane. ``active`` items are
    ``{id, kind, lane, ts, status, started_at?}``. Within a lane the running item
    (head) is position 1; each queued item's wait ≈ jobs-ahead × their medians +
    the running item's remaining time (median − elapsed). ETA to done = wait +
    its own median. Returns (per-id info, per-lane count). No client math over
    another tenant's data — the caller passes only THIS org's active jobs."""
    lanes: dict[str, list[dict]] = {}
    for j in active:
        lanes.setdefault(j.get("lane") or "local", []).append(j)
    info: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for lane, jobs in lanes.items():
        jobs.sort(key=lambda j: (0 if j.get("status") == "running" else 1, j.get("ts", 0)))
        counts[lane] = len(jobs)
        cum = 0.0  # seconds until the START of the current item
        for idx, j in enumerate(jobs):
            m = medians.get(j.get("kind", ""), default_median)
            row = {"lane": lane, "position": idx + 1, "of": len(jobs),
                   "node": j.get("node") or ("studio pod" if lane == "local" else "gpu")}
            if idx == 0 and j.get("status") == "running":
                started = j.get("started_at") or j.get("ts") or now
                elapsed = max(0, now - started)
                remaining = max(0.0, m - elapsed)
                row.update(elapsed_seconds=int(elapsed), wait_seconds=0,
                           remaining_seconds=int(remaining), eta_seconds=int(remaining))
                cum = remaining
            else:
                row.update(elapsed_seconds=0, wait_seconds=int(cum),
                           remaining_seconds=None, eta_seconds=int(cum + m))
                cum += m
            info[j["id"]] = row
    return info, counts


def build_lineage(rows: list[dict], target: str) -> dict:
    """Pure: given work-log rows (each with ``parents`` = source library paths and
    ``output`` = the landed library path, resolved by the caller) and a target
    library asset path, return its version chain:

        {"ancestors": [older … immediate parent], "node": <target>,
         "descendants": [later derivations, chronological]}

    Each entry is ``{path, prompt, kind, ts}``. Ancestors walk the first-parent edge
    up from the row that PRODUCED the target; descendants are rows that name the
    target among their parents (and, transitively, their outputs). Cycles and
    missing rows terminate the walk — never loops."""
    by_output: dict[str, dict] = {}
    for r in rows:
        out = r.get("output")
        if out and out not in by_output:
            by_output[out] = r

    def entry(path: str, r: dict | None) -> dict:
        return {
            "path": path,
            "prompt": (r or {}).get("prompt", ""),
            "kind": (r or {}).get("kind", ""),
            "ts": (r or {}).get("ts", 0),
        }

    # Ancestors: from the row that produced `target`, follow its first parent up.
    ancestors: list[dict] = []
    seen = {target}
    cur = target
    while True:
        producer = by_output.get(cur)
        if not producer:
            break
        parents = producer.get("parents") or []
        if not parents:
            break
        parent = parents[0]
        if parent in seen:
            break
        seen.add(parent)
        ancestors.append(entry(parent, by_output.get(parent)))
        cur = parent
    ancestors.reverse()  # oldest first

    # Descendants: rows that derive FROM target, then rows deriving from those, …
    descendants: list[dict] = []
    frontier = [target]
    seen_out = {target}
    while frontier:
        nxt: list[str] = []
        kids = [r for r in rows if any(p in frontier for p in (r.get("parents") or []))]
        kids.sort(key=lambda r: r.get("ts", 0))
        for r in kids:
            out = r.get("output")
            key = out or r.get("id")
            if key in seen_out:
                continue
            seen_out.add(key)
            descendants.append(entry(out or "", r))
            if out:
                nxt.append(out)
        frontier = nxt

    return {"ancestors": ancestors, "node": target, "descendants": descendants}
