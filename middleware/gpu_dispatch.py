"""Dispatch a render to the org's connected GPU node via the cloud gpu-jobs queue.

When the caller's IAM org has a live compute worker (a box running `hanzo gpu
connect` / hanzod, registered in the cloud fleet), the render runs THERE instead
of in this GPU-less pod. The user's own IAM bearer token is forwarded, so the
cloud derives org + project + linked billing account from it — one auth path, no
shared keys. Falls back to the in-pod queue when there is no live worker.
"""
import base64
import json
import os
import urllib.request

import folder_paths

CLOUD = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai").rstrip("/")
JOBS_NS = os.environ.get("STUDIO_GPU_JOBS_NS", "gpu-jobs")
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


def _has_online_gpu(tok: str) -> bool:
    """True if the caller's org has an online GPU machine in the cloud fleet."""
    req = urllib.request.Request(f"{CLOUD}/v1/machines", headers={"Authorization": tok})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return any(m.get("status") == "online" and m.get("gpu") for m in d.get("machines", []))


def dispatch_if_worker(request, org_id: str, prompt_id: str, prompt: dict) -> bool:
    """If the caller's org has an online GPU node in the fleet, enqueue a
    studio.render job to gpu-jobs and return True. Return False to run in-pod.
    Never raises — any failure falls back to local."""
    try:
        tok = _bearer(request)
        if not tok or not _has_online_gpu(tok):
            return False
        body = json.dumps({
            "activityId": prompt_id,
            "runId": prompt_id,
            "activityType": {"name": "studio.render"},
            "taskQueue": JOBS_NS,
            # 1200s: a heavy Qwen edit on a cold GB10 (model reload + full 30-step
            # sample) can exceed 600s; the worker heartbeats each step, so this is
            # the ceiling for a genuinely long single render, not idle slack.
            "heartbeatTimeout": "1200s",
            "input": {
                "prompt": prompt,
                "org": org_id,
                "uploadUrl": STUDIO_PUBLIC_URL,
                # Uploaded inputs live in orgs/{org}/input on THIS pod; the worker
                # renders on its own disk and cannot read them — ship them along so
                # LoadImage resolves there.
                "inputs": _collect_input_images(prompt, org_id),
            },
        }).encode()
        req = urllib.request.Request(
            f"{CLOUD}/v1/tasks/namespaces/{JOBS_NS}/activities",
            data=body,
            headers={"Authorization": tok, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()
        _track_dispatched(org_id, prompt_id, prompt)
        return True
    except Exception:
        return False


def _track_dispatched(org_id: str, prompt_id: str, prompt: dict) -> None:
    """Record a BYO-GPU render dispatched to gpu-jobs so the studio UI can SHOW it
    in the queue. BYO-GPU jobs render on the worker (gpu-jobs), not the pod's local
    /queue — so without this the user's fix/compose 'never appears in the queue'.
    A tiny per-org JSON the /v1/render-queue endpoint reads; entries drop off when
    their output lands in the library or they age out. Never raises."""
    try:
        import time
        import folder_paths
        # human title from the SaveImage prefix; refs from LoadImage for a thumbnail
        prefix, refs = "render", []
        for n in prompt.values():
            if not isinstance(n, dict):
                continue
            ct = n.get("class_type", "")
            if ct == "SaveImage":
                prefix = (n.get("inputs", {}).get("filename_prefix") or "render").split("/")[-1]
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
        jobs.append({"id": prompt_id, "prefix": prefix, "refs": refs[:1],
                     "ts": int(time.time()), "status": "rendering"})
        tmp = jf + ".tmp"
        open(tmp, "w").write(json.dumps(jobs))
        os.replace(tmp, jf)
    except Exception:
        pass
