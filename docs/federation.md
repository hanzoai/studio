# Hanzo Studio — GPU Federation

How `studio.hanzo.ai` spreads generation across a heterogeneous GPU pool:
in-cluster cloud pods **and** local boxes (e.g. a GB10) that join over an
**outbound-only** connection. No inbound tunnel to a local box is ever required.

## 1. A Studio process holds no queue

A box renders one prompt at a time, and nothing waits behind it. `POST /prompt`
either starts the render or is refused (`503 renderer_busy`); there is no
backlog in this process to lose to a crash, to replay on boot, or to persist.

That is the whole storage design. No snapshot file, no SQLite job table, no
in-memory heap of pending work — a render is either happening or it is not, and
`GET /prompt` answers exactly that:

```json
{"exec_info": {"rendering": 0}}
```

The renderer (`execution.Renderer`) owns the one slot, the `PromptExecutor`
behind it, and the history of what has finished. Every render runs on the same
thread, so the executor's model cache and the CUDA context stay where they were
built. Memory is given back when the box has been idle for ten seconds, so a
burst of renders pays for one collect rather than one each.

## 2. The queue is the cloud's

Work that has nowhere to run right now lines up in **one** place: the cloud
tasks engine's `gpu-jobs` namespace. It is the only queue in the system, and it
belongs to the fabric, not to any Studio process.

```
client → POST /prompt (coordinator pod)
  gpu_dispatch.dispatch_if_worker(...)
    → POST {cloud}/v1/tasks/namespaces/gpu-jobs/activities
    → returns the machine it landed on
  BYO worker claims it → POST /v1/worker/execute (that box) → renders
```

- **Targeting** — the UI's "Run on \<gpu\>" chip sets `X-Target-GPU`. A targeted
  render enqueues on that machine's own lane `taskQueue="gpu:<identity>"`; an
  untargeted one rides the shared `gpu-jobs` lane any worker drains. The
  namespace is `gpu-jobs` either way — targeting is a taskQueue, not a second
  queue.
- **The dispatcher names where it went.** `dispatch_if_worker` returns the node
  identity (or `None` for an in-pod render), `POST /prompt` puts it in the
  response as `node`, and the work log records it. No side file is written for
  the caller to read back.
- **The visible list is the engine's list.** `GET /v1/render-queue` reads the
  org's activities straight from the tasks engine, so each row carries the REAL
  claiming GPU (`activity.identity`) and REAL status. Nothing can render on a
  GPU without appearing there, and there is no second copy to drift from it.
- **A refusal is a refusal.** If the fabric will not take a render, the dispatch
  fails with its reason and a `failed` work-log row. Nothing is spooled for a
  later sweep — a job the engine will never accept must say so, not wait for a
  condition that has already been met.

## 3. Engine selector **[implemented]**

An org chooses which execution target ("engine") its prompts run on. Engines are
derived from the per-org compute registry:

- `local` — this Studio instance (always present), and
- each registered org GPU worker (from `compute.json`'s worker registry).

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/engines` | List engines for the org + the current default. |
| PUT | `/v1/engines/default` | Set the org's default engine. Body `{"engine": "<id>"}`. |

The default is stored on the org's compute profile
(`ComputeProfile.default_engine`). A stale default (worker deregistered) falls
back to `local`, never 500.

Files: `middleware/engine_selector.py`, `middleware/compute_config.py`.

## 4. Security **[implemented]**

- **User auth** — all user-facing routes require an IAM JWT (JWKS-verified); org
  comes from the `owner` claim. See `middleware/iam_auth_middleware.py`.
- **Worker trust** — the IAM-exempt `/v1/worker/execute` and
  `/v1/workers/register` require `X-Worker-Token == STUDIO_WORKER_TOKEN`
  (`worker_client.verify_worker_token`). KMS-sourced in cloud; empty in a
  single-trust-domain local dev, where the check is skipped.
- **No hidden runs** — in `--worker-mode` a direct `POST /prompt` without the
  coordinator token is refused 403. The only accepted execution is a claimed job
  handed in over `POST /v1/worker/execute`
  (`worker_client.reject_untrusted_worker_submit`, one policy gate).
- **Org isolation** — a render carries its `org_id` in `extra_data`; the worker
  binds it as the execution org so outputs land in that org's namespace (§5). A
  worker only serves its own `STUDIO_ORG_ID`.

## 5. Output persistence

A worker renders on its own disk and mirrors finished outputs back to the
coordinator over `POST /v1/library/upload` with the user's IAM token — no S3 or
rclone credential ever touches the box. Uploaded inputs travel with the job as
URLs the worker fetches, so a 4K source is as enqueueable as a thumbnail.

## 6. Joining a local box (GB10)

`hanzo gpu connect --studio-dir <path>` registers the box in the fleet and
claims from the `gpu-jobs` lane, outbound-only. The box runs the same binary as
the coordinator; `--worker-mode` makes it a render backend and nothing else.
