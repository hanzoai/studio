# Hanzo Studio — GPU Federation

How `studio.hanzo.ai` runs generation jobs durably and spreads them across a
heterogeneous GPU pool: in-cluster cloud pods **and** local boxes (e.g. a GB10)
that join over an **outbound-only** connection. No inbound tunnel to a local box
is ever required.

Sections are marked **[implemented]** (code in this repo today), **[fast-path]**
(retained in-cluster optimization), or **[future]**.

## 1. Roles

- **Coordinator** — the `studio.hanzo.ai` deployment. Full UI, IAM auth,
  multi-tenant storage. Accepts `/prompt`, owns the durable render queue and the
  per-org worker registry + engine selection.
- **Worker** — a Studio process that executes prompts. Either the same box as the
  coordinator (local render), an in-cluster pod reached by push (§2a), or a box
  sharing the coordinator's render queue.

A worker is the *same binary* as the coordinator.

## 2. Durable render queue **[implemented]**

The render queue is crash-durable: pending and in-flight prompts survive a
process crash, and a killed render is re-run instead of being lost. Storage is a
single **SQLite** file (stdlib `sqlite3`, WAL) — **zero external processes**, per
the house rule (SQLite embedded by default; Postgres only for prod multi-instance).

Enable with `STUDIO_QUEUE_DB=<path>`. `server.py` then swaps `PromptQueue` for
`SqlitePromptQueue`; the existing prompt-executor thread is the claim loop
unchanged. The `PromptQueue` seam maps onto a durable job state machine:

```
put(item)             → INSERT job (pending)              # idempotent on prompt_id
get(timeout)          → claim next pending (claimed, leased)   # BEGIN IMMEDIATE
task_done(success)    → job → done
task_done(error)      → job → pending (retry) | failed    # per STUDIO_TASKS_MAX_ATTEMPTS
```

Guarantees:

- **Exactly-once submit** — the row is keyed by `prompt_id` (`INSERT OR IGNORE`);
  a resubmit of the same prompt is a no-op.
- **Exactly-once claim** — `get()` claims under `BEGIN IMMEDIATE`, so with several
  Studio processes on the same `STUDIO_QUEUE_DB` exactly one claims each job.
- **Crash-recovery** — a claim carries a lease. A background heartbeat renews it
  while the render runs; if the process dies, the lease expires and the reap step
  (run on every `get()`, in the heartbeat thread, and on boot) returns the job to
  `pending` for another claim. A render retry is idempotent — SaveImage suffixes
  increment.

Files: `middleware/tasks_queue.py` (`SqlitePromptQueue`), `server.py` (queue
selection). Precedence:

| Precedence | Trigger | Backend | Durability |
|---|---|---|---|
| 1 | `STUDIO_QUEUE_DB` set | `SqlitePromptQueue` (one file) | Crash-durable, exactly-once, retry, multi-process |
| 2 | `STUDIO_PERSIST_QUEUE=1` | `PromptQueue` + JSON snapshot | Single-box crash-survival (re-queue on boot) |
| 3 | (default) | `PromptQueue` (in-memory) | None — same as upstream ComfyUI |

Env: `STUDIO_QUEUE_DB` (path), `STUDIO_TASKS_QUEUE` (default `studio-render`),
`STUDIO_TASKS_MAX_ATTEMPTS` (default 3), `STUDIO_TASKS_LEASE_MS` (default 60000,
heartbeat renews at lease/3), `STUDIO_WORKER_ID` (default `<host>-<pid>`).

### 2a. Push — in-cluster fast path **[fast-path]**

When the coordinator can already reach a worker (both pods in `hanzo-k8s`, or a
routable `WORKER_EXTERNAL_URL`), it may forward a prompt directly:

```
client → POST /prompt (coordinator)
  prompt_router.route_prompt(org, body)
    → get_available_gpu_worker(org)         # compute_config registry
    → POST {worker.url}/v1/worker/execute   # forward_to_worker
```

Files: `middleware/prompt_router.py`, `middleware/worker_client.py`. This path is
not durable on its own (a forwarded prompt lost to a worker crash is not retried);
keep it for latency-sensitive in-cluster dispatch. It requires the coordinator to
reach the worker, so it cannot serve a NAT'd box.

## 3. Engine selector **[implemented]**

An org chooses which execution target ("engine") its prompts run on. Engines are
derived from what already exists in the per-org compute registry:

- `local` — this Studio instance (always present), and
- each registered org GPU worker (from `compute.json`'s worker registry).

Endpoints (coordinator):

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/engines` | List engines for the org + the current default. |
| PUT | `/v1/engines/default` | Set the org's default engine. Body `{"engine": "<id>"}`. |

The default is stored on the org's compute profile (`ComputeProfile.default_engine`).
`prompt_router.route_prompt` honors it: if the default names a live worker, the
prompt is forwarded there; `local` (the default) leaves current behavior
unchanged. A stale default (worker deregistered) falls back to `local`, never 500.

Files: `middleware/engine_selector.py`, `middleware/compute_config.py`,
`middleware/prompt_router.py`. **Frontend TODO:** a queue-panel/settings dropdown
to pick the engine — the API + per-org default ship now; the UI control is a
follow-up.

## 4. Security **[implemented]**

- **User auth** — all user-facing routes require an IAM JWT (JWKS-verified); org
  comes from the `owner` claim. See `middleware/iam_auth_middleware.py`.
- **Worker trust** — the IAM-exempt `/v1/worker/execute` and `/v1/workers/register`
  require `X-Worker-Token == STUDIO_WORKER_TOKEN` (`worker_client.verify_worker_token`).
  KMS-sourced in cloud; empty in a single-trust-domain local dev, where the check
  is skipped.
- **Org isolation** — a render carries its `org_id` in `extra_data`; the worker
  binds it as the execution org so outputs land in that org's namespace (§5). A
  worker only serves its own `STUDIO_ORG_ID`.

## 5. Output persistence **[future]**

Workers execute against their own output dir (org-scoped via
`folder_paths.set_execution_org`). For the coordinator UI to serve results across
boxes, workers upload outputs to `s3.lux.cloud` (hanzo bucket), key
`studio/{org_id}/{prompt_id}/{filename}`, and the coordinator serves via `/view`
(or a signed redirect). Until then, results remain on the executing worker —
fine for the single-box and in-cluster cases.

## 6. Future direction

The durable queue, engine selection, and dispatch are deliberately kept in a thin
Python layer at the `PromptQueue`/`prompt_router` seams so the backend can be
swapped without touching the render path. Two moves are planned: (a) a **remote
queue backend** — the same job state machine served by **Hanzo Tasks**
(`github.com/hanzoai/tasks`) over its HTTP surface, so multiple coordinators and
NAT'd `--worker`-style claimers share one durable queue instead of a local file;
and (b) folding that scheduling/queue layer into a **Go subsystem inside the
`hanzoai/cloud` unified binary** (HIP-0106), leaving Studio's Python as a pure
execution worker. The engine selector will likewise grow a third class,
**leased cloud machines** provisioned via the platform's Visor compute surface
(`GET {cloud}/v1/machines`) — the `/v1/engines` shape already accommodates the
extra entries; the client interface is stubbed in `engine_selector.py` and wired
when the console lands.

## 7. Joining a local box (GB10)

Two paths today:

- **Shared queue** (durable): point the box at the same `STUDIO_QUEUE_DB` (shared
  volume) as the coordinator; its prompt-executor thread claims jobs directly.
  Exactly-once claim + crash-recovery hold across processes.
- **Push** (in-cluster, fast): `python main.py --worker-mode --coordinator-url … --worker-id gb10-1`
  with `STUDIO_WORKER_TOKEN`; requires the coordinator to reach the box.

The remote-queue backend (§6) generalizes the shared-queue path to boxes that
cannot share a filesystem, outbound-only.
