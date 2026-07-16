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


def record(org: str, *, kind: str, prompt: str, refs: list, uploads: list,
           parents: list, output_prefix: str, params: dict | None = None,
           pid: str | None = None, lane: str | None = None,
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
