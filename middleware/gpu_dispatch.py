"""Dispatch a render to the org's connected GPU node via the cloud gpu-jobs queue.

When the caller's IAM org has a live compute worker (a box running `hanzo gpu
connect` / hanzod, registered in the cloud fleet), the render runs THERE instead
of in this GPU-less pod. The user's own IAM bearer token is forwarded, so the
cloud derives org + project + linked billing account from it — one auth path, no
shared keys. Falls back to the in-pod queue when there is no live worker.
"""
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

import folder_paths

log = logging.getLogger("studio.gpu_dispatch")

CLOUD = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai").rstrip("/")
JOBS_NS = os.environ.get("STUDIO_GPU_JOBS_NS", "gpu-jobs")

# The ComfyUI output-node class types — the ONE Python list, defined in this base
# module (no studio_home dependency) so studio_home can import it without a cycle.
# The client mirror lives in studio_home.html (a separate JS runtime).
SAVE_NODES = ("SaveImage", "SaveVideo", "SaveWEBM")
# Public base of THIS studio deployment — where the BYO-GPU worker returns finished
# outputs (POST /upload/output → orgs/{org}/output → gallery). The worker uploads
# here with the user's IAM token; no S3/rclone credentials ever touch the box.
STUDIO_PUBLIC_URL = os.environ.get("STUDIO_PUBLIC_URL", "https://studio.hanzo.ai").rstrip("/")

# Node classes whose `image`/`mask` widget names a file in the org input dir. The
# render runs on the worker's LOCAL disk, so any file a user UPLOADED here (it lands
# in orgs/{org}/input, which the worker cannot read) must travel WITH the job.
_IMAGE_INPUT_CLASSES = ("LoadImage", "LoadImageMask", "LoadImageOutput")
# Cap the inlined payload so a pathological graph can't blow the tasks-API body.
_MAX_INLINE_BYTES = 48 * 1024 * 1024


def _collect_input_images(prompt: dict, org_id: str) -> list[dict]:
    """Read every LoadImage-referenced file that exists under this org's input
    directory and return them base64-encoded for transport to the worker. Files
    already resident on the worker (e.g. staged garment refs) are harmless to
    resend — the worker overwrites byte-identical copies. Best-effort: an absent
    or oversized file is skipped, never fatal."""
    in_dir = folder_paths.get_org_input_directory(org_id)
    seen: set[str] = set()
    out: list[dict] = []
    total = 0
    for node in prompt.values():
        if not isinstance(node, dict) or node.get("class_type") not in _IMAGE_INPUT_CLASSES:
            continue
        name = (node.get("inputs") or {}).get("image")
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        # Honor a "sub/dir/file.png" reference; reject traversal.
        rel = os.path.normpath(name)
        if rel.startswith("..") or os.path.isabs(rel):
            continue
        path = os.path.join(in_dir, rel)
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if size == 0 or total + size > _MAX_INLINE_BYTES:
                continue
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        total += len(data)
        sub, base = os.path.split(rel)
        out.append({"name": base, "subfolder": sub, "data": base64.b64encode(data).decode()})
    return out


def _bearer(request) -> str:
    """The caller's IAM token, exactly as iam_auth_middleware accepts it:
    Authorization header first, then the browser session cookies."""
    h = request.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h
    tok = request.cookies.get("hanzo_token") or request.cookies.get("access_token")
    return f"Bearer {tok}" if tok else ""


# Identity keys a fleet machine might carry, most human-friendly first. We READ
# whatever the gpu-jobs lane publishes (home-lab labels like spark/dbc/evo land in
# `name`/`hostname`); we never invent a parallel registry.
_NODE_KEYS = ("name", "hostname", "label", "node", "worker_id", "machine_id", "id", "gpu_model")


def _node_label(m: dict) -> str:
    for k in _NODE_KEYS:
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "gpu"


def _fleet_machines(tok: str) -> list:
    """The caller-org's machines from the cloud fleet. The token scopes the fleet to
    the caller's org, so this is inherently tenant-isolated. Retried a few times so a
    transient edge fault (502, TLS reset, timeout) does NOT read as "no GPU" and
    bounce the render onto the model-less pod — the cause of intermittent
    "Prompt outputs failed validation"."""
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{CLOUD}/v1/machines", headers={"Authorization": tok})
            d = json.loads(urllib.request.urlopen(req, timeout=10).read())
            ms = d.get("machines", [])
            return ms if isinstance(ms, list) else []
        except Exception as e:  # any transient fault (HTTP/TLS/timeout/non-JSON) is retryable
            last = e
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise last


def _online_gpu_nodes(tok: str) -> list:
    return [m for m in _fleet_machines(tok) if m.get("status") == "online" and m.get("gpu")]


def _has_online_gpu(tok: str) -> bool:
    """True if the caller's org has an online GPU machine in the cloud fleet."""
    return bool(_online_gpu_nodes(tok))


# Server credentials that read the WHOLE fleet, not just the caller-org's slice like
# a user token. KMS-sourced (the SAME envs worker/commerce already use); consulted
# ONLY by the advisory capacity gate below — never by a render decision.
_SERVICE_TOKEN_ENVS = ("STUDIO_WORKER_TOKEN", "STUDIO_COMMERCE_TOKEN")


def _service_token() -> str:
    """A service bearer for a fleet read, or "" if none is configured."""
    for env in _SERVICE_TOKEN_ENVS:
        v = os.environ.get(env, "").strip()
        if v:
            return v if v.lower().startswith("bearer ") else f"Bearer {v}"
    return ""


def has_online_gpu_advisory(user_tok: str) -> bool:
    """Fleet availability for the UI capacity gate ONLY (the ⚠ warning + the
    /v1/gpu-status badge) — NEVER a render decision. Reads with a SERVICE identity
    first so every org sees the TRUE worker availability: a BYO box is often
    registered under a sibling org and is invisible to an org-scoped user-token read,
    which made a karma session believe no GPU existed. Falls back to the user token
    when no service credential is set (local dev). Job tenancy is untouched — dispatch
    still enqueues under the user token."""
    for tok in (_service_token(), user_tok):
        if not tok:
            continue
        try:
            if _has_online_gpu(tok):
                return True
        except Exception:
            continue
    return False


# Model-loader class → (the widget naming the file, the folder_paths list it lives in).
# Covers the loaders the studio graphs use: Qwen fix/compose (UNET+CLIP+VAE) and the
# SD-family templates (checkpoint). Enough to answer "can THIS box load THIS graph".
_LOADERS = {
    "UNETLoader": ("unet_name", "diffusion_models"),
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "CheckpointLoader": ("ckpt_name", "checkpoints"),
    "ImageOnlyCheckpointLoader": ("ckpt_name", "checkpoints"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "VAELoader": ("vae_name", "vae"),
}


def has_models_for(prompt: dict) -> bool:
    """True only if THIS instance has EVERY model file the graph actually references
    (its UNET / checkpoint / CLIP / VAE loader inputs). The decision is per-GRAPH, not
    "any model present": a cloud coordinator may carry a stray checkpoint (e.g. an SD1.5
    stopgap) yet lack the Qwen edit models a fix/compose graph needs — validating THAT
    locally is the cryptic "not in []" / "Prompt outputs failed validation". So a box
    renders locally only a graph it can actually load; every other graph goes to the
    fleet (or a retryable 503). Errs toward the fleet (returns False) on any doubt. A
    worker node (worker_mode) never reaches this path."""
    try:
        for node in (prompt or {}).values():
            if not isinstance(node, dict):
                continue
            spec = _LOADERS.get(node.get("class_type", ""))
            if not spec:
                continue
            name = (node.get("inputs") or {}).get(spec[0])
            if not isinstance(name, str) or not name:
                continue
            try:
                have = folder_paths.get_filename_list(spec[1])
            except Exception:
                have = []
            if name not in have:
                return False   # a model this graph needs is absent → cannot render here
        return True
    except Exception:
        return False


# Retry ladder across the cloud tasks backend's deploy gap. `/v1/tasks/*` is served
# by the single-pod `cloud` binary (strategy Recreate on an RWO SQLite volume), so
# every cloud deploy leaves a ~30s window where the ingress (Traefik) has zero Ready
# endpoints and returns 503 "no available server" for the enqueue. With CI shipping
# cloud continuously, an enqueue can land in that gap and instant-fail the render with
# "GPU render worker is momentarily unavailable". The gap is transient and the request
# never reached the tasks engine (empty backend pool → Traefik rejects it), so we
# retry across it. ~32s total covers a same-node Recreate.
_ENQUEUE_RETRY_DELAYS = (2, 4, 6, 8, 12)


def _enqueue_with_retry(req) -> None:
    """PUT the enqueue, retrying ONLY on 503 / connection-refused — both mean the
    request never reached the backend, so re-sending the same idempotent activityId
    (== prompt_id) cannot double-enqueue. Any other outcome (4xx auth/validation, a
    timeout that may have been processed) raises to the caller's normal fallback, so
    we never risk a double render on an ambiguous failure."""
    last: Exception | None = None
    for i in range(len(_ENQUEUE_RETRY_DELAYS) + 1):
        try:
            urllib.request.urlopen(req, timeout=90).read()
            return
        except urllib.error.HTTPError as e:
            if e.code != 503:
                raise            # 4xx/other: retrying won't help — fall back
            last = e             # 503 empty-pool: deploy gap, retriable
        except urllib.error.URLError as e:
            # Connection refused/reset to the backend during the gap is retriable;
            # a genuine timeout (request may have been served) is NOT — don't double-send.
            if isinstance(e.reason, TimeoutError):
                raise
            last = e
        if i < len(_ENQUEUE_RETRY_DELAYS):
            time.sleep(_ENQUEUE_RETRY_DELAYS[i])
    if last:
        raise last


def dispatch_if_worker(request, org_id: str, prompt_id: str, prompt: dict) -> bool:
    """Enqueue this render to the shared GPU lane (gpu-jobs) and return True; return
    False only to run it in-pod.

    A model-less cloud coordinator NEVER renders locally — it ALWAYS enqueues and
    lets the job WAIT for a worker, even when the org-scoped fleet read shows none.
    BYO workers can be registered under a sibling org while draining the SHARED
    gpu-jobs queue, so an org-blind read (e.g. an org whose GPUs live under a peer
    org) must not bounce the render onto the GPU-less pod — that self-POST is what
    died with an instant "Prompt outputs failed validation". A queued job that waits
    for a worker beats an instant fake failure. A box WITH its own models still
    renders locally when it sees no online worker (local dev / a worker node).

    Never raises — a genuine enqueue failure falls back, and server.py then returns a
    retryable 503 on a model-less pod rather than a doomed local validation."""
    try:
        tok = _bearer(request)
        if not tok:
            return False
        # Per-GPU targeting: the UI's "Run on <gpu>" affordance sets X-Target-GPU to a
        # machine identity. A targeted render is enqueued on THAT machine's own lane
        # ("gpu:<identity>") which only that worker claims; an untargeted render rides
        # the shared "gpu-jobs" lane any worker drains. The NAMESPACE is "gpu-jobs"
        # either way — targeting is a taskQueue, not a second queue. One enqueue path.
        target = (request.headers.get("X-Target-GPU") or "").strip()
        # The fleet read is ADVISORY (targeting + node label). A machines-API blip
        # must never abort the enqueue itself — with no fleet view the job simply
        # rides the shared lane and the claimer decides the node.
        try:
            online = _online_gpu_nodes(tok)
        except Exception as e:                              # noqa: BLE001 — advisory read
            log.warning("fleet read failed; enqueuing to the shared lane: %s", e)
            online = []
        # Honor a target ONLY if it is one of the caller-org's OWN online GPUs. This
        # blocks a forged lane AND sanitizes the value before it becomes a taskQueue
        # (and later a queue-row label): an unknown/offline target falls back to the
        # shared lane rather than enqueuing onto a "gpu:<bogus>" lane no worker will
        # ever claim (a 30-min schedule-to-start hang) — and a "<img onerror>" target
        # never reaches the queue. Cross-org is already impossible (per-org shard); this
        # is defense in depth.
        if target and target not in {_node_label(m) for m in online}:
            target = ""
        task_queue = f"gpu:{target}" if target else JOBS_NS
        # No visible worker AND this box can load THIS graph → render locally. No
        # visible worker on a box that lacks the graph's models → still enqueue and
        # wait (never a doomed local validation), even with a stray unrelated model.
        # A validated target ALWAYS enqueues (the user asked for that online GPU).
        if not target and not online and has_models_for(prompt):
            return False
        # Which node runs it: the explicit target; else a single visible GPU is
        # definitive; with several — or an org-blind read that saw none — the claimer
        # is decided at pick-up: "gpu".
        node = target or (_node_label(online[0]) if len(online) == 1 else "gpu")
        staged = _collect_input_images(prompt, org_id)
        body = json.dumps({
            "activityId": prompt_id,
            "runId": prompt_id,
            "activityType": {"name": "studio.render"},
            "taskQueue": task_queue,
            # 3600s. The heartbeat rides SAMPLING steps, so the window must also cover
            # everything BEFORE the first step: on a box whose models thrash (Qwen-Edit,
            # Z-Image, Wan and Hunyuan3D all competing for one host's memory) a cold load
            # is minutes on its own, and the worker's own readiness probe may kill and
            # reload studio mid-load — restarting that clock. At 1200s six real renders
            # were reclaimed as "lease expired: worker unresponsive" while the GPU was
            # busy, then re-run, then reclaimed again: the queue reported failure and
            # burned the GPU twice for one output. A genuinely dead worker is still
            # reclaimed, just later — a late reclaim costs one wait, a premature one
            # costs the render.
            "heartbeatTimeout": "3600s",
            # Liveness is the heartbeat above; this cap only bounds a live-but-stuck
            # render. Unset, the tasks default (~1h) reaped real renders mid-run and
            # the queue re-ran them for hours.
            "startToCloseTimeout": "14400s",
            # A job with no GPU online WAITS — queued work runs when a worker
            # connects, it never silently evaporates (the product contract). The
            # old 30-min expiry "protected" against a backlog replaying on
            # reconnect, but it did so by silently discarding the user's work; the
            # protection now is HONESTY — the queue shows every waiting job the
            # whole time and Cancel genuinely cancels the activity. 7 days bounds
            # a truly abandoned org.
            "scheduleToStartTimeout": "604800s",
            # ONE attempt, no auto-retry. A GPU render is expensive and often
            # deterministic — re-running a failed/crashed render (e.g. spark OOM mid-
            # sample) just re-burns money on the SAME output ("the same reject over and
            # over"). A failure surfaces to the user, who re-runs deliberately. This is
            # the cap that the startToCloseTimeout alone did NOT provide.
            "retryPolicy": {"maximumAttempts": 1},
            "input": {
                "prompt": prompt,
                "org": org_id,
                "uploadUrl": STUDIO_PUBLIC_URL,
                # Uploaded inputs live in orgs/{org}/input on THIS pod; the worker
                # renders on its own disk and cannot read them — ship them along so
                # LoadImage resolves there.
                "inputs": staged,
            },
        }).encode()
        req = urllib.request.Request(
            f"{CLOUD}/v1/tasks/namespaces/{JOBS_NS}/activities",
            data=body,
            headers={"Authorization": tok, "Content-Type": "application/json"},
        )
        # 90s, not 15s: a fix/compose with a large (e.g. 4k) reference inlines tens of
        # MB, and 15s wasn't enough to PUT the enqueue body — it timed out, dispatch
        # fell back, and the model-less pod returned "GPU worker unavailable" on a
        # perfectly good render. The enqueue itself is quick; this only covers upload.
        # Retry across the cloud tasks backend's deploy gap (see _enqueue_with_retry).
        _enqueue_with_retry(req)
        _track_dispatched(org_id, prompt_id, prompt, node=node)
        # Correlated by pid (the job id) with studio.home's build/queue lines; names are
        # content-addressed checksums, so this pins the exact source bytes shipped — no
        # signed URLs, tokens or image data are logged.
        log.info("studio.trace gpu.publish org=%s pid=%s node=%s inputs=%s refs=%s",
                 org_id, prompt_id, node, len(staged),
                 ",".join(sorted(s["name"] for s in staged)) or None)
        return True
    except Exception:
        return False


def _track_dispatched(org_id: str, prompt_id: str, prompt: dict, node: str = "gpu") -> None:
    """Record a BYO-GPU render dispatched to gpu-jobs so the studio UI can SHOW it
    in the queue. BYO-GPU jobs render on the worker (gpu-jobs), not the pod's local
    /queue — so without this the user's fix/compose 'never appears in the queue'.
    A tiny per-org JSON the /v1/render-queue endpoint reads; entries drop off when
    their output lands in the library or they age out. Never raises."""
    try:
        import time
        import folder_paths
        # human title from the SaveImage prefix; refs from LoadImage for a thumbnail
        full_prefix, refs = "render", []
        for n in prompt.values():
            if not isinstance(n, dict):
                continue
            ct = n.get("class_type", "")
            if ct in SAVE_NODES:
                full_prefix = n.get("inputs", {}).get("filename_prefix") or "render"
            elif "LoadImage" in ct:
                im = n.get("inputs", {}).get("image")
                if im:
                    refs.append(im)
        out_dir = folder_paths.get_org_output_directory(org_id)
        os.makedirs(out_dir, exist_ok=True)
        jf = os.path.join(out_dir, "render_jobs.json")
        try:
            jobs = json.loads(open(jf).read())
        except Exception:
            jobs = []
        jobs = [j for j in jobs if j.get("id") != prompt_id][-40:]  # dedupe + cap
        # store the FULL output prefix (e.g. "fixes/model01_..._00001_") so the queue
        # endpoint can check for the ACTUAL output file, not a fuzzy base match that
        # collides with the source image and makes the job vanish after 30s.
        jobs.append({"id": prompt_id, "prefix": full_prefix.split("/")[-1],
                     "outPrefix": full_prefix, "refs": refs[:1], "node": node,
                     "ts": int(time.time()), "status": "rendering"})
        tmp = jf + ".tmp"
        open(tmp, "w").write(json.dumps(jobs))
        os.replace(tmp, jf)
    except Exception:
        pass
