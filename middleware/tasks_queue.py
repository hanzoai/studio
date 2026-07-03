"""
SqlitePromptQueue — Hanzo Studio's crash-durable render queue.

Drop-in for execution.PromptQueue, backed by a single SQLite file (stdlib
sqlite3, WAL). When STUDIO_QUEUE_DB is set, render jobs survive a process
crash: pending and in-flight prompts live in the DB, and a killed render is
rescheduled for another claim instead of being lost. Zero external processes.

The PromptQueue seam maps straight onto a durable job state machine:

    put(item)              → INSERT job (pending)          # idempotent on prompt_id
    get(timeout)           → claim next pending (claimed, leased)
    task_done(...success)  → job → done
    task_done(...error)    → job → pending (retry) | failed  (per max attempts)

The existing prompt_worker thread is the claim loop unchanged: it calls get()
(claim) and task_done() (report). A background heartbeat renews the lease of
the in-flight job while the render runs; if this process dies, the lease
expires and the reap step (run on every get() and by the heartbeat thread)
returns the job to `pending` so it is re-claimed. A render retry is
idempotent — SaveImage suffixes increment.

Multiple Studio processes pointing at the same STUDIO_QUEUE_DB compete safely:
claim runs under BEGIN IMMEDIATE, so exactly one process claims each job.

History and flags stay in the in-memory base (the Studio UI reads them); only
the queue mechanics become durable. This supersedes STUDIO_PERSIST_QUEUE (JSON
snapshot) as the local durable path; that remains the zero-dep fallback.
Standard library only — no new hard dependency.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import socket
import sqlite3
import threading
import time

from execution import PromptQueue

logger = logging.getLogger(__name__)

DEFAULT_QUEUE = "studio-render"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS render_jobs (
  id           TEXT PRIMARY KEY,
  queue        TEXT NOT NULL,
  payload      TEXT NOT NULL,
  state        TEXT NOT NULL,              -- pending|claimed|done|failed
  attempt      INTEGER NOT NULL DEFAULT 1,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  worker       TEXT,
  lease_expiry REAL,
  result       TEXT,
  cause        TEXT,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_render_jobs_ready ON render_jobs(queue, state, created_at);
"""


def serialize_item(item: tuple) -> str:
    """Render-queue tuple → JSON job payload."""
    number, prompt_id, prompt, extra_data, outputs, sensitive = item
    return json.dumps({
        "number": number,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "extra_data": extra_data,
        "outputs": outputs,
        "sensitive": sensitive,
    })


def deserialize_item(payload: str) -> tuple:
    """JSON job payload → render-queue tuple (inverse of serialize_item)."""
    d = json.loads(payload)
    return (
        d.get("number", 0),
        d["prompt_id"],
        d["prompt"],
        d.get("extra_data") or {},
        d.get("outputs") or [],
        d.get("sensitive") or {},
    )


def _worker_identity() -> str:
    return os.environ.get("STUDIO_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


class SqlitePromptQueue(PromptQueue):
    """PromptQueue whose durability lives in a SQLite file."""

    def __init__(self, server, db_path: str | None = None):
        super().__init__(server)
        # SQLite is the durable backend — disable the JSON snapshot path and
        # drop anything a stray STUDIO_PERSIST_QUEUE restore left behind
        # (precedence: SQLite > PERSIST > memory).
        self._persist_path = None
        self.queue = []

        self.db_path = db_path or os.environ["STUDIO_QUEUE_DB"]
        self.queue_name = os.environ.get("STUDIO_TASKS_QUEUE", DEFAULT_QUEUE)
        self.worker_id = _worker_identity()
        self.max_attempts = int(os.environ.get("STUDIO_TASKS_MAX_ATTEMPTS", "3"))
        self.lease_s = float(os.environ.get("STUDIO_TASKS_LEASE_MS", "60000")) / 1000.0
        self.poll_interval = float(os.environ.get("STUDIO_TASKS_POLL_INTERVAL", "1.0"))

        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

        # item_id → job_id so task_done reports to the right row.
        self._job_by_item: dict[int, str] = {}
        self._pending = 0
        self._reap()  # recover leases orphaned by a previous crash

        logger.info(
            "[sqlite-queue] durable render queue: db=%s queue=%s worker=%s lease=%.0fs",
            self.db_path, self.queue_name, self.worker_id, self.lease_s,
        )

        self._hb_interval = max(self.lease_s / 3.0, 2.0)
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

    # ── producer: /prompt → durable job ──────────────────────────────
    def put(self, item):
        prompt_id = item[1]
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO render_jobs"
                "(id, queue, payload, state, attempt, max_attempts, created_at, updated_at)"
                " VALUES(?,?,?,'pending',1,?,?,?)",
                (prompt_id, self.queue_name, serialize_item(item), self.max_attempts, now, now),
            )
        self.server.queue_updated()

    # ── consumer: prompt_worker → claim ──────────────────────────────
    def get(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self._reap()
            claimed = self._claim()
            if claimed is not None:
                return claimed
            if deadline is not None and time.monotonic() >= deadline:
                return None
            wait = self.poll_interval
            if deadline is not None:
                wait = min(wait, max(deadline - time.monotonic(), 0.0))
            if self._hb_stop.wait(wait):
                return None

    def _claim(self):
        now = time.time()
        with self._db_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id, payload FROM render_jobs"
                    " WHERE queue=? AND state='pending' ORDER BY created_at LIMIT 1",
                    (self.queue_name,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                job_id, payload = row
                self._conn.execute(
                    "UPDATE render_jobs SET state='claimed', worker=?, lease_expiry=?, updated_at=?"
                    " WHERE id=?",
                    (self.worker_id, now + self.lease_s, now, job_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        try:
            item = deserialize_item(payload)
        except Exception as e:  # corrupt payload — fail terminally, don't loop.
            logger.error("[sqlite-queue] undecodable job %s: %s", job_id, e)
            self._terminal(job_id, "failed", cause=f"undecodable payload: {e}")
            return None
        with self.mutex:
            item_id = self.task_counter
            self.currently_running[item_id] = copy.deepcopy(item)
            self._job_by_item[item_id] = job_id
            self.task_counter += 1
        self.server.queue_updated()
        return (item, item_id)

    # ── completion → durable state ───────────────────────────────────
    def task_done(self, item_id, history_result, status, process_item=None):
        with self.mutex:
            job_id = self._job_by_item.pop(item_id, None)
        if job_id is not None:
            success = status is None or status.status_str == "success"
            if success:
                self._terminal(job_id, "done",
                               result=json.dumps({"outputs": _summarize_outputs(history_result)}))
            else:
                cause = "; ".join(status.messages) if status and status.messages else "render error"
                self._fail(job_id, cause)
        super().task_done(item_id, history_result, status, process_item)

    def _terminal(self, job_id, state, result=None, cause=None):
        with self._db_lock:
            self._conn.execute(
                "UPDATE render_jobs SET state=?, result=?, cause=?, worker=NULL,"
                " lease_expiry=NULL, updated_at=? WHERE id=?",
                (state, result, cause, time.time(), job_id),
            )

    def _fail(self, job_id, cause):
        # Retry-aware: another attempt if the policy allows, else terminal.
        with self._db_lock:
            self._conn.execute(
                "UPDATE render_jobs SET"
                " attempt = attempt + 1,"
                " state = CASE WHEN attempt + 1 > max_attempts THEN 'failed' ELSE 'pending' END,"
                " worker=NULL, lease_expiry=NULL, cause=?, updated_at=?"
                " WHERE id=? AND state='claimed'",
                (cause, time.time(), job_id),
            )

    # ── crash-recovery: reclaim expired leases ───────────────────────
    def _reap(self):
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "UPDATE render_jobs SET"
                " attempt = attempt + 1,"
                " state = CASE WHEN attempt + 1 > max_attempts THEN 'failed' ELSE 'pending' END,"
                " cause = CASE WHEN attempt + 1 > max_attempts THEN 'lease expired' ELSE cause END,"
                " worker=NULL, lease_expiry=NULL, updated_at=?"
                " WHERE state='claimed' AND lease_expiry IS NOT NULL AND lease_expiry < ?",
                (now, now),
            )

    # ── heartbeat / pending refresh ──────────────────────────────────
    def _heartbeat_loop(self):
        while not self._hb_stop.wait(self._hb_interval):
            now = time.time()
            with self.mutex:
                inflight = list(self._job_by_item.values())
            if inflight:
                with self._db_lock:
                    for job_id in inflight:
                        self._conn.execute(
                            "UPDATE render_jobs SET lease_expiry=?, updated_at=?"
                            " WHERE id=? AND state='claimed'",
                            (now + self.lease_s, now, job_id),
                        )
            self._reap()
            with self._db_lock:
                (self._pending,) = self._conn.execute(
                    "SELECT COUNT(*) FROM render_jobs WHERE queue=? AND state='pending'",
                    (self.queue_name,),
                ).fetchone()

    def get_tasks_remaining(self):
        with self.mutex:
            return len(self.currently_running) + self._pending

    def job_state(self, job_id: str):
        """Diagnostic: current (state, attempt) of a job, or None."""
        with self._db_lock:
            row = self._conn.execute(
                "SELECT state, attempt FROM render_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return row

    def stop(self):
        self._hb_stop.set()
        with self._db_lock:
            self._conn.close()


def _summarize_outputs(history_result) -> list:
    """Extract output filenames from an execution's history_result."""
    files = []
    try:
        outputs = (history_result or {}).get("outputs", {})
        for node_output in outputs.values():
            for key in ("images", "gifs", "audio", "video"):
                for entry in node_output.get(key, []) or []:
                    name = entry.get("filename")
                    if name:
                        files.append({"filename": name, "subfolder": entry.get("subfolder", ""),
                                      "type": entry.get("type", "output")})
    except Exception:
        pass
    return files
