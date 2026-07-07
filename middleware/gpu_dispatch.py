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


def _bearer(request) -> str:
    h = request.headers.get("Authorization", "")
    return h if h.lower().startswith("bearer ") else ""


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
            "input": {"prompt": prompt, "org": org_id},
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
