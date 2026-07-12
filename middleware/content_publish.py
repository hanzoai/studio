"""
Publish a completed Studio render into the Hanzo content lane.

THE one writer, and it writes NOTHING new. A finished render becomes an `Asset`
document — the DocType hanzoai/cloud `clients/content` already declares (framework
module "marketing") and already writes itself when IT drives a render
(clients/content/studio_render.go). Studio-initiated renders were the only ones
with no path into that record; this closes exactly that gap and adds no second
schema, no second lifecycle, and no second storage plane.

    POST /v1/framework/Asset   (the already-live generic framework surface)

Same shape `clients/content` writes (studio_render.go `draftAsset`):

    title, kind, design, prompt, file, generator, workflow,
    source_prompt_id, render_params, project, tags

`file` is the object key under the org's studio output prefix —
``orgs/<org>/output/<subfolder>/<filename>`` — byte-identical to the key
`persistOrLink` produces, and the same key Studio's own output mirror already
lands. So the bytes are ALREADY persisted by Studio: this module uploads nothing
and holds no storage credential. The record points at what exists.

Curation is the content lane's ONE state machine (clients/content/lifecycle.go):

    draft → in_review → approved → queued → published

An asset lands in `draft` (the DocType's default) and a human moves it. Publishing
is what puts an image in front of customers, so an unattended render must never do
it. The storefront reads `published`.

Auth is the requesting user's OWN verified IAM access token, carried to the prompt
worker on the queue's existing `sensitive` channel (execution.SENSITIVE_EXTRA_DATA_KEYS)
so it is never persisted into history. The Asset is written AS that user, into that
user's org — the framework derives the tenant from the token's `owner` claim. No
service account, no impersonation, no cross-org write path.

Fire-and-forget: a publish failure only logs. A render is NEVER failed for it.
Env-gated; absent locally = no-op.

    STUDIO_CLOUD_API_URL   Hanzo Cloud base URL (default https://api.hanzo.ai)
    STUDIO_CONTENT_PUBLISH "1"/"true" to enable
"""
import logging
import os
import posixpath

import aiohttp

CLOUD_URL = os.environ.get("STUDIO_CLOUD_API_URL", "https://api.hanzo.ai")

# One Asset row per artifact; small JSON writes.
_TIMEOUT = aiohttp.ClientTimeout(total=float(os.environ.get("STUDIO_CONTENT_TIMEOUT", "15")))

# clients/content declares Asset.kind as a Select; anything else would 422.
KINDS = ("ecom", "product", "lifestyle", "hover", "hero", "thumbnail")
DEFAULT_KIND = "product"


def enabled() -> bool:
    """True when render publishing is switched on. Absent locally = no-op."""
    return os.environ.get("STUDIO_CONTENT_PUBLISH", "").strip().lower() in ("1", "true", "yes")


def artifacts(history_result: dict | None) -> list[dict]:
    """
    The media artifacts a completed render produced, as Studio's own output refs
    ({filename, subfolder, type}). Mirrors billing_middleware.count_outputs — the
    same shape Studio already returns to the UI, so there is no second notion of
    "what a render produced". Previews (type != "output") are not artifacts.
    """
    out: list[dict] = []
    for node_output in (history_result or {}).get("outputs", {}).values():
        if not isinstance(node_output, dict):
            continue
        for values in node_output.values():
            if not isinstance(values, list):
                continue
            for v in values:
                if isinstance(v, dict) and v.get("filename") and v.get("type") == "output":
                    out.append(v)
    return out


def object_key(org_id: str, ref: dict) -> str:
    """
    The asset's key under the org's studio output prefix. Byte-identical to the key
    clients/content builds in persistOrLink (orgs/<org>/output/...), which is also
    where Studio's own output mirror lands — ONE key convention, so the record and
    the bytes can never disagree.
    """
    return posixpath.join(
        "orgs", org_id, "output", (ref.get("subfolder") or "").strip("/"), ref["filename"]
    ).replace("//", "/")


def kind_of(extra_data: dict) -> str:
    """The Asset.kind Select value. Anything off-list would be refused by the engine."""
    k = str(extra_data.get("kind") or "").strip().lower()
    return k if k in KINDS else DEFAULT_KIND


def _model(workflow: dict | None) -> str:
    """Best-effort model name off the graph's first checkpoint/UNet loader."""
    for node in (workflow or {}).values():
        if not isinstance(node, dict):
            continue
        if "Loader" not in str(node.get("class_type", "")):
            continue
        for v in (node.get("inputs") or {}).values():
            if isinstance(v, str) and v.endswith((".safetensors", ".ckpt", ".gguf", ".sft")):
                return v
    return ""


def asset_doc(*, org_id: str, prompt_id: str, workflow: dict, ref: dict, extra_data: dict) -> dict:
    """
    One artifact as an Asset document — the same field set clients/content writes.
    `status` is omitted on purpose: the DocType defaults it to draft, and the
    lifecycle hook owns every transition from there.
    """
    model = _model(workflow)
    return {
        "title": ref["filename"],
        "kind": kind_of(extra_data),
        "design": str(extra_data.get("design") or "").strip(),
        "role": str(extra_data.get("role") or "").strip(),
        "prompt": str(extra_data.get("prompt") or "").strip(),
        "file": object_key(org_id, ref),
        "generator": model or "studio",
        "workflow": model or "studio",
        "source_prompt_id": prompt_id,
        # The graph that reproduces this artifact — the reproducibility record.
        "render_params": workflow or {},
        "project": str(extra_data.get("project") or "").strip(),
        "tags": str(extra_data.get("tags") or "").strip(),
    }


async def publish_render(
    *,
    org_id: str,
    prompt_id: str,
    workflow: dict,
    history_result: dict | None,
    iam_token: str | None,
    extra_data: dict | None = None,
) -> int:
    """
    Publish a completed render's artifacts as draft Assets. Returns how many landed.

    Never raises: the caller is the prompt worker, and a render must not fail
    because the content lane was unreachable.
    """
    if not enabled():
        return 0
    if not iam_token:
        # No verified user principal on this execution. We refuse to invent one:
        # writing via a service account would be a cross-org write path.
        logging.warning("content_publish: no IAM token on prompt %s — not published", prompt_id)
        return 0

    refs = artifacts(history_result)
    if not refs:
        return 0
    extra = extra_data or {}
    url = f"{CLOUD_URL.rstrip('/')}/v1/framework/Asset"
    headers = {"Authorization": f"Bearer {iam_token}"}
    landed = 0

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            for ref in refs:
                doc = asset_doc(
                    org_id=org_id, prompt_id=prompt_id, workflow=workflow,
                    ref=ref, extra_data=extra,
                )
                async with session.post(url, json=doc, headers=headers) as resp:
                    if resp.status in (200, 201):
                        landed += 1
                    else:
                        logging.warning(
                            "content_publish: POST /v1/framework/Asset -> %s: %s",
                            resp.status, (await resp.text())[:200],
                        )
    except Exception as e:  # unreachable cloud / timeout — a render is never failed for this
        logging.warning("content_publish: prompt %s not published: %s", prompt_id, e)

    return landed
