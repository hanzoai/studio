# Hanzo Studio — service & crash operations

How Hanzo Studio runs on the GB10 box, how to read crash history, and the fix
for the restart-storm class of outage.

## Service topology — ONE canonical unit

Hanzo Studio runs as exactly **one** systemd unit, version-controlled in this
repo:

- Canonical source: **`ops/systemd/studio.service`**
- Installed at: `/etc/systemd/system/studio.service` (system unit, `User=z`)
- Serves on `0.0.0.0:8188`, enabled at boot (`WantedBy=multi-user.target`)

There must be **exactly one** unit bound to :8188. Today's outage was a
**duplicate-unit collision**: a transient/user `hanzo-studio.service` (from
`systemd-run`) was created alongside `studio.service`; both had `Restart=` and
both tried to bind :8188, so they ping-ponged the port in a restart storm
(`OSError: [Errno 98] address already in use`). The fix is to keep only the
canonical unit and never re-create a second one.

```bash
systemctl status studio.service              # health
sudo systemctl restart studio.service         # graceful restart
journalctl -u studio.service -f               # live logs
curl -s http://127.0.0.1:8188/prompt          # {"exec_info":{"rendering":0}}
```

Roll out code or unit changes **without interrupting a running render** (waits
until nothing is rendering, removes any stray duplicate, reinstalls the canonical
unit, restarts, health-checks):

```bash
scripts/studio-service-swap.sh
```

## Reading crash history

stdout/stderr go to journald (no separate log file). To inspect crashes:

```bash
# Everything, newest last:
journalctl -u studio.service -o short-precise --no-pager

# Just failures / restarts / shutdowns:
journalctl -u studio.service --no-pager | grep -E 'Traceback|Error|Failed|Main process exited|Scheduled restart|shutting down'

# How did the last process exit? (0 = clean, 1 = crash, killed/9 = SIGKILL/OOM)
journalctl -u studio.service --no-pager | grep 'Main process exited' | tail
systemctl show studio.service -p ExecMainStatus -p NRestarts -p Result

# Kernel OOM / GPU VRAM exhaustion:
sudo dmesg -T | grep -iE 'out of memory|killed process|oom-kill|NV_ERR_NO_MEMORY'
```

## The crash that ate the history (root cause + fix)

**Symptom** (seen 3×): on any SIGTERM/SIGINT the process died with

```
File "main.py", line 507, in <module>
    event_loop.run_until_complete(x)
RuntimeError: Event loop stopped before Future completed.
```

and the in-memory history was lost.

**Root cause.** The fork's signal handler (`_graceful_shutdown` in `main.py`,
added in `a45a441`) called `event_loop.stop()`. The server coroutine passed to
`run_until_complete()` never finishes on its own, so stopping the loop makes
`run_until_complete()` raise `RuntimeError('Event loop stopped before Future
completed.')`. The surrounding `try` only caught `KeyboardInterrupt`, so the
process exited **non-zero with a traceback** instead of shutting down cleanly.
Because the exit was dirty, the socket
was sometimes not released before restart, producing secondary
`OSError: [Errno 98] address already in use` crashes; the duplicate unit turned
that into a storm.

**Fix (`main.py`).** The handler now runs once (idempotent), then stops the
loop; the outer `try` treats the resulting
`RuntimeError('Event loop stopped before Future completed.')` as the expected
graceful-exit path and logs `Server stopped by shutdown signal`. Verified live:

```
Received SIGTERM, shutting down gracefully...
Server stopped by shutdown signal
```

**Fix (topology).** The duplicate transient/user `hanzo-studio.service` was
removed; `studio.service` is the single canonical unit.

## A hard kill loses at most the render in flight

A graceful signal is handled, but a SIGKILL/OOM can still land mid-render. Only
that one render is lost, because that is all the box was holding: work waiting
for a GPU lives in the cloud `gpu-jobs` lane (see `docs/federation.md`), which
survives any single box and hands the job to whichever worker claims next.

There is nothing local to snapshot or restore, and nothing to re-run on boot.
The earlier design snapshotted pending prompts to `user/queue_snapshot.json`
and replayed them at start; it also replayed the prompt that had just killed the
process, so a box that could not run a graph would restore it, die, and restore
it again.

## Known-benign shutdown noise

During interpreter teardown you may see a one-shot traceback:

```
File "studio/model_management.py", line 800, in cleanup_models
    if current_loaded_models[i].real_model() is None:
TypeError: 'NoneType' object is not callable
```

This is an atexit/weakref finalizer racing module teardown (upstream ComfyUI).
It is cosmetic — it prints after the server has already stopped and does not
affect the render or the exit path.
