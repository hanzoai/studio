"""Dispatch a render to the org's connected GPU node via the cloud gpu-jobs queue.

When the caller's IAM org has a live compute worker (a box running `hanzo gpu
connect` / hanzod, registered in the cloud fleet), the render runs THERE instead
of in this GPU-less pod. The user's own IAM bearer token is forwarded, so the
cloud derives org + project + linked billing account from it — one auth path, no
shared keys. Falls back to the in-pod queue when there is no live worker.
"""
import json
import os
import urllib.request

CLOUD = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai").rstrip("/")
JOBS_NS = os.environ.get("STUDIO_GPU_JOBS_NS", "gpu-jobs")
# Public base of THIS studio deployment — where the BYO-GPU worker returns finished
# outputs (POST /upload/output → orgs/{org}/output → gallery). The worker uploads
# here with the user's IAM token; no S3/rclone credentials ever touch the box.
STUDIO_PUBLIC_URL = os.environ.get("STUDIO_PUBLIC_URL", "https://studio.hanzo.ai").rstrip("/")


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
            "heartbeatTimeout": "600s",
            "input": {"prompt": prompt, "org": org_id, "uploadUrl": STUDIO_PUBLIC_URL},
        }).encode()
        req = urllib.request.Request(
            f"{CLOUD}/v1/tasks/namespaces/{JOBS_NS}/activities",
            data=body,
            headers={"Authorization": tok, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False
