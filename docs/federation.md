# Hanzo Studio — GPU Federation

How `studio.hanzo.ai` schedules generation jobs across a heterogeneous GPU pool:
in-cluster cloud pods **and** local boxes (e.g. a GB10) that join over an
**outbound-only** connection. No inbound tunnel to the local box is ever
required.

This document is the contract. Sections are marked **[implemented]** (code in
this repo today) or **[specced]** (agreed design, not yet wired). Nothing here is
a stub in code — specced pieces live in this doc until they are built.

## 1. Roles

- **Coordinator** — the `studio.hanzo.ai` deployment. Full UI, IAM auth,
  multi-tenant storage. Owns the per-org worker registry and the job schedule.
  Runs a small CPU footprint (it routes; it does not need a GPU).
- **Worker** — a headless Studio started with `--worker-mode`. Reports its
  device (GPU model, VRAM), registers with the coordinator, and executes prompts
  it is given. A worker belongs to exactly one org (`STUDIO_ORG_ID`).

A worker is the *same binary* as the coordinator; `--worker-mode` only drops the
UI/websocket surface and turns on registration + the execute endpoint. This is
the ComfyUI-Distributed lineage (workers are full ComfyUI instances the control
plane drives) narrowed to **job-level** dispatch and hardened for multi-tenant +
NAT traversal.

## 2. Two dispatch models

Reachability, not preference, decides which model a worker uses.

### 2a. Push — for in-cluster workers **[implemented]**

The coordinator can open a connection to the worker (both are pods in
`hanzo-k8s`, or the worker has a routable `WORKER_EXTERNAL_URL`).

```
client → POST /prompt (coordinator)
  prompt_router.route_prompt(org, body)
    → get_available_gpu_worker(org)         # compute_config registry
    → POST {worker.url}/v1/worker/execute   # forward_to_worker
  worker queues + executes locally, returns {prompt_id, ...}
```

Files: `middleware/prompt_router.py`, `middleware/worker_client.py`
(`add_worker_routes` → `/v1/worker/execute`), `server.py` (`/prompt` calls
`route_prompt`).

Limitation: requires the coordinator to reach the worker. A GB10 behind NAT is
**not** reachable, so push cannot serve it.

### 2b. Pull — for NAT'd local boxes (the GB10 pattern) **[specced]**

The worker dials **out** and keeps the connection; the coordinator pushes jobs
down that already-open channel. No inbound port on the box.

Primary transport is a WebSocket the worker opens to the coordinator:

```
worker → WSS {coordinator}/v1/workers/connect      (Authorization: worker token)
  ← {type:"job", job_id, prompt, extra_data, org_id}
  → {type:"progress", job_id, value, max}
  → {type:"result", job_id, status, outputs:[{name, s3_url}...]}
```

HTTP long-poll fallback for environments without WSS:

```
worker → POST /v1/jobs/claim   {worker_id, org_id}     # long-poll, ≤30s
  ← 204 (no work) | 200 {job_id, prompt, extra_data}
worker → POST /v1/jobs/{job_id}/result {status, outputs}
```

The coordinator keeps a per-org, per-worker job queue (a natural extension of
`compute_config`), enqueues on `/prompt` when the target worker is pull-mode, and
hands the job to whichever channel the worker holds open. `route_prompt` gains a
third branch (`{"action":"queued"}` → `202`) alongside the existing
forward/provisioning/unavailable branches.

Not implemented because it does not "drop out" of ComfyUI's `--listen`
architecture — it needs a real coordinator-side queue and a WS control channel.
Building it does not change the endpoints below.

## 3. Endpoints

All under `/v1/` (also served under the `/api/v1/` alias ComfyUI adds for the
frontend proxy). Worker↔coordinator endpoints are exempt from user IAM and
carry the coordinator shared secret instead (`X-Worker-Token`, §5).

| Method | Path | Dir | Status | Purpose |
|---|---|---|---|---|
| POST | `/v1/workers/register` | worker→coord | **impl** | Register / heartbeat. Body: `{worker_id, url, org_id, device, gpu_model, vram_gb, status}`. |
| GET  | `/v1/workers` | client→coord | **impl** | List the org's workers + liveness. |
| POST | `/v1/worker/execute` | coord→worker | **impl** | Push a prompt to a reachable worker. Same body as `/prompt`. |
| WSS  | `/v1/workers/connect` | worker→coord | **spec** | Persistent pull channel for NAT'd workers. |
| POST | `/v1/jobs/claim` | worker→coord | **spec** | HTTP long-poll fallback to claim a job. |
| POST | `/v1/jobs/{id}/result` | worker→coord | **spec** | Report result + artifact URLs. |
| GET  | `/v1/compute/config` | client→coord | **impl** | Read the org's compute profile. |
| PUT  | `/v1/compute/config` | client→coord | **impl** | Update profile (GPU tier, auto-provision). |
| POST | `/v1/compute/provision` | client→coord | **impl** | Autoscale a cloud GPU worker via Visor. |

(Compute-profile + Visor autoscale routes are still under `/compute/*` in
`server.py`; the `/v1/compute/*` labels above are the canonical names to fold to
when those handlers are next touched. Worker-registry + execute already moved.)

## 4. Registration & scheduling **[implemented]**

- Worker sends a heartbeat every 30s (`WorkerClient._heartbeat_loop`).
- Coordinator upserts it into the org's `compute.json`
  (`compute_config.register_worker`), pruning workers silent >300s.
  Liveness for scheduling is 90s (`WorkerInfo.is_alive`).
- `get_available_gpu_worker(org)` returns the first ready, alive CUDA worker.
- `prompt_router.route_prompt` chooses local vs worker from the prompt's
  `device_preference` (`auto`|`cpu`|`gpu`) and the org's `gpu_enabled`.
- If no worker and the org has `auto_provision`, Visor launches a cloud GPU VM
  that boots a worker (`middleware/visor_client.py`).

## 5. Security

- **User auth** — all user-facing routes require an IAM JWT (JWKS-verified);
  org comes from the `owner` claim. See `middleware/iam_auth_middleware.py`.
- **Worker trust** — worker↔coordinator endpoints are IAM-exempt (they carry no
  user) and instead require `X-Worker-Token == STUDIO_WORKER_TOKEN`
  (`worker_client.verify_worker_token`). The token is KMS-sourced in cloud;
  empty in a single-trust-domain local dev, where the check is skipped.
  **[specced]** forward direction: replace the shared secret with an IAM
  service-account token (`hanzo-studio` client-credentials grant) so each worker
  is individually attributable and revocable.
- **Org isolation** — a job carries its `org_id`; the worker binds it as the
  execution org so outputs land in that org's namespace (§6). A worker only ever
  claims jobs for its own `STUDIO_ORG_ID`.
- **No inbound** — pull-mode boxes expose nothing; the coordinator is reached
  over TLS via `hanzoai/ingress`.

## 6. Output persistence **[specced]**

Push/pull both execute on the worker, which writes to *its own* output dir
(org-scoped via `folder_paths.set_execution_org`). For the coordinator UI to
show results, workers must ship artifacts to shared storage:

- On completion, upload outputs to `s3.lux.cloud` (hanzo bucket),
  key `studio/{org_id}/{prompt_id}/{filename}`.
- Report the object URLs in the `result` message; the coordinator records them
  in history so `/view` (or a signed redirect) serves them.

Until this lands, push-mode results remain on the worker and are addressable
only if the worker is reachable — acceptable for the in-cluster pool, blocking
for the NAT pull pool. This is the top build item after the pull channel.

## 7. Joining a local box (GB10)

```bash
python main.py \
  --worker-mode \
  --coordinator-url https://studio.hanzo.ai \
  --worker-id gb10-1
# env: STUDIO_ORG_ID=<org>  STUDIO_WORKER_TOKEN=<from KMS>
```

Today (push) this works only if the coordinator can reach the box (set
`WORKER_EXTERNAL_URL`). Once §2b lands, the box needs **no** inbound access: it
registers, opens the WSS channel, and pulls jobs for its org.

## 8. Build order

1. Pull WSS channel `/v1/workers/connect` + coordinator per-worker queue (§2b).
2. Artifact upload to `s3.lux.cloud` + result recording (§6).
3. IAM service-account worker identity replacing the shared secret (§5).
4. Fold `/compute/*` profile routes to `/v1/compute/*` (§3).
