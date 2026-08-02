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
            assert len(json.load(f)["items"]) == 2

        # Start rendering 'a' (highest priority == lowest number). It moves to
        # currently_running; the snapshot must still cover BOTH so an in-flight
        # crash re-queues it.
        item, ident = q1.get(timeout=1)
        assert item[1] == "a"
        with open(snap) as f:
            assert len(json.load(f)["items"]) == 2

        # Finish 'a'. Snapshot now holds only the still-pending 'b'.
        q1.task_done(ident, {}, None)
        with open(snap) as f:
            assert [row[1] for row in json.load(f)["items"]] == ["b"]

        # Simulate a restart: a brand-new PromptQueue must re-queue 'b'.
        q2 = PromptQueue(_StubServer())
        running, queued = q2.get_current_queue_volatile()
        assert running == []
        assert len(queued) == 1 and queued[0][1] == "b"
        # Restored item is a real queue tuple that can be popped and run.
        item2, _ = q2.get(timeout=1)
        assert item2[1] == "b" and isinstance(item2[2], dict)
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
    finally:
        os.environ.pop("STUDIO_PERSIST_QUEUE", None)


if __name__ == "__main__":
    # Standalone smoke run: each test raises AssertionError on failure, so a
    # clean exit (status 0) means all three passed.
    test_persist_and_restore_round_trip()
    test_disabled_by_default_is_inert()
    test_restore_skips_corrupt_rows()


def test_a_prompt_that_keeps_killing_the_process_is_dropped(tmp_path, monkeypatch):
    """Restoring the in-flight prompt must not become an infinite crash loop.

    Re-queueing what was running is the point of this feature: a crash mid-render
    must not lose the user's work. But a prompt the box CANNOT run turns that into a
    loop — restored, kills the process, restored again. Observed on a worker whose
    ROCm stack dies loading the Qwen VAE: seven restarts in thirty minutes, the node
    useless throughout, and nothing in the queue explaining why.

    After _MAX_RESTORE_ATTEMPTS the prompt is dropped and said so, and — critically —
    the count is persisted at RESTORE time, because the crash being guarded against
    happens before any later snapshot would have written it.
    """
    monkeypatch.setenv("STUDIO_PERSIST_QUEUE", "1")
    monkeypatch.setattr(folder_paths, "get_user_directory", lambda: str(tmp_path))

    q = PromptQueue(_StubServer())
    q.put(_item(0, "poison"))
    assert len(json.load(open(tmp_path / "queue_snapshot.json"))["items"]) == 1

    # Each boot restores it; the process dies before finishing it, every time.
    seen = []
    for _ in range(PromptQueue._MAX_RESTORE_ATTEMPTS + 1):
        q2 = PromptQueue(_StubServer())
        seen.append(len(q2.queue))

    assert seen[0] == 1, "the first restore must happen — that is the whole feature"
    assert seen[-1] == 0, f"a repeatedly-fatal prompt must stop being restored: {seen}"
    # Dropped for good: the snapshot no longer offers it to the next boot either.
    assert len(PromptQueue(_StubServer()).queue) == 0


def test_a_healthy_prompt_is_still_restored_after_one_crash(tmp_path, monkeypatch):
    """The guard must not punish the ordinary case it exists to serve.

    A power cut, an OOM, a deploy mid-render — one restart is normal, and the work
    must come back.
    """
    monkeypatch.setenv("STUDIO_PERSIST_QUEUE", "1")
    monkeypatch.setattr(folder_paths, "get_user_directory", lambda: str(tmp_path))
    q = PromptQueue(_StubServer())
    q.put(_item(0, "ordinary"))
    assert len(PromptQueue(_StubServer()).queue) == 1, "one restart must restore the work"
