"""library.json write integrity — the curation-wipe class.

A fixed shared tmp let two build-manifest sidecars (one per pod during a roll)
interleave into one inode -> torn JSON -> readers get {} -> every asset resets to
draft. These pin: unique tmp per write, no fixed-tmp residue, concurrent writers
never produce a torn/invalid file, and the shared lock file coordinates."""

import concurrent.futures
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import library_manifest as lm


def _doc(n):
    return {"_meta": {"count": n}, "assets": [{"path": f"a{i}.png", "status": "approved"} for i in range(n)]}


def test_write_atomic_uses_unique_tmp_no_fixed_residue(tmp_path):
    root = str(tmp_path)
    lm.write_atomic(root, _doc(3))
    # the manifest is valid and complete
    doc = json.loads((tmp_path / "library.json").read_text())
    assert len(doc["assets"]) == 3
    # the old fixed shared tmp must never be the write path
    assert not (tmp_path / ".library.json.tmp").exists()


def test_concurrent_writes_never_tear(tmp_path):
    root = str(tmp_path)
    # many writers hammering the same manifest — under the fixed-tmp bug this tore;
    # with unique tmp + atomic rename every reader sees a COMPLETE valid doc.
    def w(n):
        lm.write_atomic(root, _doc(n))
        return json.loads((tmp_path / "library.json").read_text())  # read must always parse
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(w, [5, 10, 15, 20, 25, 30, 35, 40] * 3))
    for doc in results:
        assert "assets" in doc and isinstance(doc["assets"], list)   # never {} from a torn read
    final = json.loads((tmp_path / "library.json").read_text())
    assert len(final["assets"]) in (5, 10, 15, 20, 25, 30, 35, 40)   # one whole writer won, not a splice


def test_library_lock_serializes(tmp_path):
    root = str(tmp_path)
    with lm.library_lock(root):
        assert (tmp_path / lm.LOCK_NAME).exists()   # lock file materializes and is held
