"""
Standalone tests for SqlitePromptQueue — Hanzo Studio's crash-durable render
queue. A fake torch-free `execution` module supplies the PromptQueue base, so
this runs with no GPU, no torch, and no external process (stdlib sqlite3 only).

Covers the queue seam (put/get/task_done), exactly-once claim, retry-on-error,
and — the headline — crash-recovery: a job claimed and then abandoned by a
"crashed" queue instance is reaped and re-run to completion by a fresh instance
on the same DB file.
"""
import sys
import threading
import types
from collections import namedtuple

import pytest

ExecStatus = namedtuple("ExecStatus", ["status_str", "completed", "messages"])


def _fake_execution_module():
    mod = types.ModuleType("execution")

    class PromptQueue:
        def __init__(self, server):
            self.server = server
            self.mutex = threading.RLock()
            self.task_counter = 0
            self.queue = []
            self.currently_running = {}
            self.history = {}
            self.flags = {}
            self._persist_path = None

        def task_done(self, item_id, history_result, status, process_item=None):
            self.currently_running.pop(item_id, None)

    mod.PromptQueue = PromptQueue
    return mod


class FakeServer:
    def __init__(self):
        self.updates = 0

    def queue_updated(self):
        self.updates += 1


@pytest.fixture
def tqmod(monkeypatch):
    monkeypatch.setenv("STUDIO_TASKS_POLL_INTERVAL", "0.05")
    monkeypatch.setenv("STUDIO_TASKS_LEASE_MS", "300")  # short lease → fast reap
    monkeypatch.setenv("STUDIO_TASKS_MAX_ATTEMPTS", "3")
    saved = {k: sys.modules.get(k) for k in ("execution", "middleware.tasks_queue")}
    sys.modules["execution"] = _fake_execution_module()
    sys.modules.pop("middleware.tasks_queue", None)
    import middleware.tasks_queue as tq
    try:
        yield tq
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _item(pid="prompt-1"):
    return (0, pid, {"1": {"class_type": "EmptyImage"}}, {"org_id": "acme"}, ["1"], {})


def _queue(tqmod, tmp_path, freeze_hb=True):
    import os
    os.environ["STUDIO_QUEUE_DB"] = str(tmp_path / "queue.db")
    q = tqmod.SqlitePromptQueue(FakeServer())
    if freeze_hb:
        q._hb_stop.set()  # deterministic: no background reaper/heartbeat
    return q


def test_serde_roundtrip(tqmod):
    assert tqmod.deserialize_item(tqmod.serialize_item(_item())) == _item()


def test_put_get_task_done(tqmod, tmp_path):
    q = _queue(tqmod, tmp_path)
    q.put(_item())
    assert q.server.updates == 1
    item, item_id = q.get(timeout=1.0)
    assert item == _item()
    assert q.currently_running[item_id] == _item()
    assert q.job_state("prompt-1")[0] == "claimed"
    q.task_done(item_id, {"outputs": {"2": {"images": [{"filename": "x_00001_.png"}]}}},
                ExecStatus("success", True, []))
    assert q.job_state("prompt-1")[0] == "done"
    assert item_id not in q._job_by_item


def test_put_is_idempotent(tqmod, tmp_path):
    q = _queue(tqmod, tmp_path)
    q.put(_item())
    q.put(_item())  # same prompt_id → INSERT OR IGNORE, no duplicate
    q.get(timeout=1.0)
    assert q.get(timeout=0.2) is None  # only one job existed


def test_exactly_once_claim(tqmod, tmp_path):
    q = _queue(tqmod, tmp_path)
    q.put(_item())
    first = q.get(timeout=1.0)
    assert first is not None
    assert q.get(timeout=0.2) is None  # already claimed — no double dispatch


def test_error_retries_then_fails(tqmod, tmp_path):
    q = _queue(tqmod, tmp_path)
    q.put(_item())  # max_attempts=3
    # attempt 1 → error → back to pending (attempt 2)
    _, item_id = q.get(timeout=1.0)
    q.task_done(item_id, {}, ExecStatus("error", False, ["boom-1"]))
    assert q.job_state("prompt-1") == ("pending", 2)
    # attempt 2 → error → pending (attempt 3)
    _, item_id = q.get(timeout=1.0)
    q.task_done(item_id, {}, ExecStatus("error", False, ["boom-2"]))
    assert q.job_state("prompt-1") == ("pending", 3)
    # attempt 3 → error → terminal failed
    _, item_id = q.get(timeout=1.0)
    q.task_done(item_id, {}, ExecStatus("error", False, ["boom-3"]))
    assert q.job_state("prompt-1")[0] == "failed"


def test_crash_recovery_reap(tqmod, tmp_path):
    import time
    q = _queue(tqmod, tmp_path)
    q.put(_item())
    _, _item_id = q.get(timeout=1.0)          # claimed, leased
    assert q.job_state("prompt-1")[0] == "claimed"
    time.sleep(0.4)                            # lease (0.3s) expires: worker "crashed"
    assert q.get(timeout=1.0) is not None      # get() reaps + re-claims for a new attempt
    assert q.job_state("prompt-1") == ("claimed", 2)


def test_crash_recovery_across_instances(tqmod, tmp_path):
    """The headline: a job in flight when a queue instance dies is picked up
    and completed by a fresh instance on the same DB file."""
    import time
    db = str(tmp_path / "queue.db")
    import os
    os.environ["STUDIO_QUEUE_DB"] = db

    # Instance A: submit, claim, then "crash" (drop the instance without completing).
    a = tqmod.SqlitePromptQueue(FakeServer())
    a._hb_stop.set()
    a.put(_item())
    a.get(timeout=1.0)
    assert a.job_state("prompt-1")[0] == "claimed"
    a.stop()                                   # process dies mid-render

    time.sleep(0.4)                            # lease expires

    # Instance B: same DB. Recovers the orphaned job and finishes it.
    b = tqmod.SqlitePromptQueue(FakeServer())  # __init__ reaps on boot
    b._hb_stop.set()
    item, item_id = b.get(timeout=1.0)
    assert item == _item()
    assert b.job_state("prompt-1") == ("claimed", 2)  # attempt 2 on the new worker
    b.task_done(item_id, {"outputs": {}}, ExecStatus("success", True, []))
    assert b.job_state("prompt-1")[0] == "done"
    b.stop()
