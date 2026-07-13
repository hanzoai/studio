# zen-video — headless text-to-video queue (GB10)

Fronts Hanzo Studio (ComfyUI, `:8188`) on the spark GB10 box with a small,
serialized, persisted queue that turns a prompt into a real MP4. Public model
name is the **Zen video family** (`zen3-video`); the diffusion backend is the
Zen video model (Wan2.2 TI2V-5B). The upstream/private model filenames live
only in `zen_wan_workflow.py` and are NEVER returned to callers.

## Why a queue

The GB10 is one GPU — one generation at a time. A single async worker drains an
ordered queue and submits to Studio, which itself serializes on the GPU. Jobs
persist in SQLite (`jobs.db`) so a service restart never loses in-flight or
finished state; interrupted jobs are re-queued on boot.

## Two API surfaces (composable, one backend)

1. **OpenAI-style** — for direct/frontend use, gated by gateway-injected identity:
   - `POST /v1/videos/generations` `{model, prompt, size?, seconds?, seed?, negative_prompt?}`
     → `202 {id, status:"queued", ...}`. Requires `X-Org-Id` (the gateway injects
     it from the IAM JWT); anonymous is refused unless `ZEN_ALLOW_ANON=1`.
   - `GET /v1/videos/generations/{id}` → status; on completion `{status:"completed", url}`.
   - `GET /v1/videos/files/{name}` → the MP4 (served over the Cloudflare tunnel).
   - Bills per generated second into the `usage` table (and posts to `ZEN_METER_URL` if set).

2. **do-ai-compatible async** — driven by the cloud LLM gateway (`hanzoai/ai`
   `GenerateVideoDOAI`), gated by a Bearer provider key (`ZEN_PROVIDER_KEY`):
   - `POST /v1/videos` `{model, prompt, size?, seconds?}` → `202 {id:"video_…", object:"video", status:"queued"}`
   - `GET /v1/videos/{id}` → `{status:"queued|in_progress|completed|failed"}`
   - `GET /v1/videos/{id}/content` → raw `video/mp4` (only once completed)

   This is the exact Sora-style create→poll→download contract the ai service's
   video client expects, so the ai registry adopts spark by pointing the
   `zen3-video` provider at `https://spark-video.hanzo.ai/v1` — no ai-side
   client change.

`GET /healthz` reports comfy up/down + queue depth. `GET /v1/videos/models`
lists the Zen video SKUs.

## Models

| id | default | max s | steps | $/s |
|----|---------|-------|-------|-----|
| `zen3-video` | 1280×704 | 5 | 20 | 0.20 |
| `zen3-video-fast` | 704×704 | 3 | 16 | 0.08 |
| `zen3-video-pro` | 1280×704 | 5 | 30 | 0.40 |
| `wan2-2-t2v-a14b` | 704×704 | 3 | 20 | 0.20 | (do-ai upstream id, moderate default to fit the gateway's 300s ceiling) |

## Deploy (spark, systemd)

Runs under the Studio venv (`aiohttp`, `boto3`). Unit: `zen-video.service`
(`Requires=studio.service`). Secrets in `/etc/zen-video.env` (chmod 600):
`ZEN_PROVIDER_KEY` (KMS `hanzo/video/SPARK_VIDEO_PROVIDER_KEY`).

```
sudo cp zen-video.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now zen-video.service
```

Reachability: spark is LAN-only/NAT'd. The `lux-dchain` cloudflared tunnel routes
`spark-video.hanzo.ai → localhost:8189` (TLS terminated by Cloudflare).

## Delivery

S3-first (SeaweedFS) when `ZEN_S3_*` is set, else the MP4 is served from this
service over the tunnel at `/v1/videos/files/{name}` (a scoped, unguessable URL).
NOTE: the cluster SeaweedFS admin identity is currently a placeholder
(`PLACEHOLDER`/`PLACEHOLDER`) — provision real creds in KMS to enable S3.

## Ops

`studio_clean_restart.sh` breaks a Studio EADDRINUSE/JIT crash-loop: it stops the
service, frees `:8188` of orphans, waits for memory to settle, and starts one
clean instance (studio's `comfy_kitchen` backend JIT-compiles CUDA kernels once
on first clean boot; a crash-loop never lets that finish → runaway `cicc`
compilers exhaust unified memory).
