"""Tests for the crash-durable PromptQueue snapshot (STUDIO_PERSIST_QUEUE=1).

Verifies that pending + in-flight prompts survive a process restart: they are
snapshotted to disk on every queue change and re-queued when a fresh PromptQueue
is constructed. Also verifies the feature is inert when the flag is unset.

Runs under pytest, or standalone:  python prompt_queue_persist_test.py
"""
import json
import os
import sys
import tempfile

# Import from the repo root regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import folder_paths  # noqa: E402
from execution import PromptQueue  # noqa: E402


class _StubServer:
    """PromptQueue only calls server.queue_updated(); everything else is unused."""
    def queue_updated(self):
        pass


def _item(number, prompt_id):
    # Mirrors ComfyUI's queue tuple: (number, prompt_id, prompt, extra_data, outputs)
    return (number, prompt_id, {"1": {"class_type": "Noop", "inputs": {}}}, {"client_id": "t"}, ["1"])


def _fresh_user_dir():
    d = tempfile.mkdtemp(prefix="studio-persist-test-")
    folder_paths.set_user_directory(d)
    return os.path.join(d, "queue_snapshot.json")


def test_persist_and_restore_round_trip():
    os.environ["STUDIO_PERSIST_QUEUE"] = "1"
    snap = _fresh_user_dir()
    try:
        q1 = PromptQueue(_StubServer())
        assert q1._persist_path == snap
        assert not os.path.exists(snap), "no snapshot should exist before first change"

        q1.put(_item(1, "a"))
        q1.put(_item(2, "b"))
        assert os.path.exists(snap), "put() must write a snapshot"
        with open(snap) as f:
            assert len(json.load(f)) == 2

        # Start rendering 'a' (highest priority == lowest number). It moves to
        # currently_running; the snapshot must still cover BOTH so an in-flight
        # crash re-queues it.
        item, ident = q1.get(timeout=1)
        assert item[1] == "a"
        with open(snap) as f:
            assert len(json.load(f)) == 2

        # Finish 'a'. Snapshot now holds only the still-pending 'b'.
        q1.task_done(ident, {}, None)
        with open(snap) as f:
            assert [row[1] for row in json.load(f)] == ["b"]

        # Simulate a restart: a brand-new PromptQueue must re-queue 'b'.
        q2 = PromptQueue(_StubServer())
        running, queued = q2.get_current_queue_volatile()
        assert running == []
        assert len(queued) == 1 and queued[0][1] == "b"
        # Restored item is a real queue tuple that can be popped and run.
        item2, _ = q2.get(timeout=1)
        assert item2[1] == "b" and isinstance(item2[2], dict)
        print("OK test_persist_and_restore_round_trip")
    finally:
        os.environ.pop("STUDIO_PERSIST_QUEUE", None)


def test_disabled_by_default_is_inert():
    os.environ.pop("STUDIO_PERSIST_QUEUE", None)
    snap = _fresh_user_dir()
    q = PromptQueue(_StubServer())
    assert q._persist_path is None
    q.put(_item(1, "a"))
    q.get(timeout=1)
    assert not os.path.exists(snap), "flag unset -> nothing written to disk"
    print("OK test_disabled_by_default_is_inert")


def test_restore_skips_corrupt_rows():
    os.environ["STUDIO_PERSIST_QUEUE"] = "1"
    snap = _fresh_user_dir()
    try:
        # One valid row, plus junk that must be ignored without raising.
        with open(snap, "w") as f:
            json.dump([[3, "good", {"1": {}}, {}, ["1"]], "garbage", [1, 2], {"x": 1}], f)
        q = PromptQueue(_StubServer())
        _, queued = q.get_current_queue_volatile()
        assert len(queued) == 1 and queued[0][1] == "good"
        print("OK test_restore_skips_corrupt_rows")
    finally:
        os.environ.pop("STUDIO_PERSIST_QUEUE", None)


if __name__ == "__main__":
    test_persist_and_restore_round_trip()
    test_disabled_by_default_is_inert()
    test_restore_skips_corrupt_rows()
    print("\nALL PASS")
