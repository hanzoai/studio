"""Tests for recording a completed render as an Asset in the Hanzo content lane.

These are CONTRACT tests. They stand up a real aiohttp server that speaks the
exact contract hanzoai/cloud serves — the generic framework surface
(POST /v1/framework/Asset) — and drive the real publisher against it over real
HTTP. The server side of that contract (the Asset DocType, its draft default, the
lifecycle hook that owns every status edge, cross-org isolation) is proven
independently against the real engine and a real SQLite by the content lane's own
Go tests in hanzoai/cloud (clients/content/content_test.go).

What is asserted here is Studio's half: that a finished render produces exactly one
Asset per output artifact, in the SAME field shape clients/content itself writes
(so a studio-initiated render is indistinguishable from a lane-initiated one); that
`file` is the org-scoped output key (the bytes Studio already persisted — this
module uploads nothing); that every call is authenticated AS THE REQUESTING USER;
that assets land as drafts (never auto-published to a storefront); that a refusal
comes back WHOLE (a 403 from a role-less principal is propagated with its remedy,
never swallowed into a plausible success); and that a render is never failed when
the lane is unreachable.
"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from middleware import content_publish

pytestmark = pytest.mark.asyncio  # module is all-async

TOKEN = "the-users-iam-access-token"
PROMPT_ID = "b6f1c0e2-0000-4000-8000-000000000001"
WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {"steps": 30}},
            "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-edit-2511.safetensors"}}}
HISTORY = {"outputs": {"9": {"images": [
    {"filename": "life1_00001_.png", "subfolder": "swimwear/lifestyle2/valentina", "type": "output"},
    {"filename": "life1_00002_.png", "subfolder": "swimwear/lifestyle2/valentina", "type": "output"},
]}, "10": {"images": [
    {"filename": "preview.png", "subfolder": "", "type": "temp"},  # a preview is never an artifact
]}}}
EXTRA = {"design": "valentina", "kind": "lifestyle", "role": "life1",
         "prompt": "poolside, golden hour", "project": "fashion", "tags": "valentina,life1"}


class Content:
    """A stand-in for hanzoai/cloud's framework surface, module 'marketing'."""

    def __init__(self, fail: bool = False):
        self.assets: list[dict] = []
        self.fail = fail

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/framework/Asset", self._asset)
        return app

    async def _asset(self, request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "valid principal required"}, status=403)
        body = await request.json()
        if self.fail:
            return web.json_response({"error": "boom"}, status=500)
        # The lane defaults status to draft; the client does not (and must not) set it.
        doc = {**body, "name": "hash-id", "doctype": "Asset", "status": "draft"}
        self.assets.append(doc)
        return web.json_response(doc, status=201)


async def _publish(content, aiohttp_client, monkeypatch, **kw):
    monkeypatch.setenv("STUDIO_CONTENT_PUBLISH", "1")
    client: TestClient = await aiohttp_client(TestServer(content.app()))
    monkeypatch.setattr(content_publish, "CLOUD_URL", str(client.make_url("")).rstrip("/"))
    args = {"org_id": "karma", "prompt_id": PROMPT_ID, "workflow": WORKFLOW,
            "history_result": HISTORY, "iam_token": TOKEN, "extra_data": EXTRA}
    args.update(kw)
    return await content_publish.publish_render(**args)


# --- pure helpers --------------------------------------------------------

async def test_artifacts_are_outputs_only_never_previews():
    refs = content_publish.artifacts(HISTORY)
    assert [r["filename"] for r in refs] == ["life1_00001_.png", "life1_00002_.png"]


async def test_object_key_is_the_org_scoped_output_key():
    ref = {"filename": "life1_00001_.png", "subfolder": "swimwear/lifestyle2/valentina", "type": "output"}
    assert content_publish.object_key("karma", ref) == \
        "orgs/karma/output/swimwear/lifestyle2/valentina/life1_00001_.png"
    # no subfolder collapses cleanly (no doubled slash)
    assert content_publish.object_key("karma", {"filename": "x.png", "subfolder": "", "type": "output"}) == \
        "orgs/karma/output/x.png"


async def test_kind_must_be_a_valid_select_value():
    assert content_publish.kind_of({"kind": "lifestyle"}) == "lifestyle"
    assert content_publish.kind_of({"kind": "nonsense"}) == "product"  # off-list -> the default
    assert content_publish.kind_of({}) == "product"


# --- the happy path ------------------------------------------------------

async def test_records_one_asset_per_artifact(aiohttp_client, monkeypatch):
    content = Content()
    assert await _publish(content, aiohttp_client, monkeypatch) == {"landed": 2, "errors": []}
    assert len(content.assets) == 2
    assert [a["title"] for a in content.assets] == ["life1_00001_.png", "life1_00002_.png"]


async def test_asset_matches_the_content_lane_field_shape(aiohttp_client, monkeypatch):
    content = Content()
    await _publish(content, aiohttp_client, monkeypatch)
    a = content.assets[0]
    # The exact fields clients/content writes (studio_render.go draftAsset).
    assert a["file"] == "orgs/karma/output/swimwear/lifestyle2/valentina/life1_00001_.png"
    assert a["kind"] == "lifestyle"
    assert a["design"] == "valentina"
    assert a["role"] == "life1"
    assert a["source_prompt_id"] == PROMPT_ID
    assert a["render_params"] == WORKFLOW  # the graph reproduces it
    assert a["generator"] == "qwen-image-edit-2511.safetensors"
    assert a["project"] == "fashion" and a["tags"] == "valentina,life1"


async def test_assets_land_as_draft_never_auto_published(aiohttp_client, monkeypatch):
    """A render must never push an image to a storefront. The client never sends a
    status; the lane defaults it to draft; a human moves it through the lifecycle."""
    content = Content()
    await _publish(content, aiohttp_client, monkeypatch)
    assert all("status" not in c for c in [content_publish.asset_doc(
        org_id="karma", prompt_id=PROMPT_ID, workflow=WORKFLOW, ref=r, extra_data=EXTRA
    ) for r in content_publish.artifacts(HISTORY)])
    assert all(a["status"] == "draft" for a in content.assets)


# --- auth ----------------------------------------------------------------

async def test_written_as_the_requesting_user(aiohttp_client, monkeypatch):
    # The stub 403s any call without the user's exact bearer, so a full publish is
    # itself proof every write carried it.
    content = Content()
    assert (await _publish(content, aiohttp_client, monkeypatch))["landed"] == 2


async def test_forbidden_is_propagated_never_swallowed(aiohttp_client, monkeypatch):
    """The lane refuses `create on Asset` unless the principal holds Content Editor.
    Studio cannot grant that (the write runs as the caller, by design), so the 403 must
    come back WHOLE — real status, upstream body, and the one action that resolves it."""
    content = Content()
    res = await _publish(content, aiohttp_client, monkeypatch, iam_token="a-role-less-principal")
    assert res["landed"] == 0 and content.assets == []
    err = res["errors"][0]
    assert err["status"] == 403                                  # not degraded to 200/0
    assert "valid principal required" in err["message"]          # the upstream body, verbatim
    assert content_publish.FORBIDDEN_REMEDY in err["message"]    # …and how to fix it
    assert err["path"] == "orgs/karma/output/swimwear/lifestyle2/valentina/life1_00001_.png"


async def test_without_a_user_token_nothing_is_written(aiohttp_client, monkeypatch):
    content = Content()
    res = await _publish(content, aiohttp_client, monkeypatch, iam_token=None)
    assert res["landed"] == 0 and res["errors"][0]["status"] == 401
    assert content.assets == []


# --- a render is never failed --------------------------------------------

async def test_disabled_is_a_no_op(aiohttp_client, monkeypatch):
    content = Content()
    monkeypatch.delenv("STUDIO_CONTENT_PUBLISH", raising=False)
    monkeypatch.setattr(content_publish, "CLOUD_URL", "http://127.0.0.1:1")
    # enabled() reads the env directly, so call the real entrypoint with it unset.
    res = await content_publish.publish_render(
        org_id="karma", prompt_id=PROMPT_ID, workflow=WORKFLOW,
        history_result=HISTORY, iam_token=TOKEN, extra_data=EXTRA,
    )
    assert res["landed"] == 0 and res["errors"][0]["status"] == 503
    assert content.assets == []


async def test_lane_error_is_survivable(aiohttp_client, monkeypatch):
    content = Content(fail=True)
    res = await _publish(content, aiohttp_client, monkeypatch)        # nothing landed, no raise
    assert res["landed"] == 0
    assert [e["status"] for e in res["errors"]] == [500, 500]         # one per artifact, reported


async def test_unreachable_lane_never_raises(monkeypatch):
    monkeypatch.setenv("STUDIO_CONTENT_PUBLISH", "1")
    monkeypatch.setattr(content_publish, "CLOUD_URL", "http://127.0.0.1:1")  # nothing listening
    res = await content_publish.publish_render(
        org_id="karma", prompt_id=PROMPT_ID, workflow=WORKFLOW,
        history_result=HISTORY, iam_token=TOKEN, extra_data=EXTRA,
    )
    assert res["landed"] == 0 and res["errors"][0]["status"] == 502


async def test_empty_render_writes_nothing(aiohttp_client, monkeypatch):
    content = Content()
    assert await _publish(content, aiohttp_client, monkeypatch,
                          history_result={"outputs": {}}) == {"landed": 0, "errors": []}
    assert content.assets == []


# --- the token never leaks into history ----------------------------------

async def test_iam_token_is_a_sensitive_key_so_it_is_stripped_from_history():
    # Force the CPU device path before the heavy import: model_management probes
    # CUDA at import time and a GPU-less CI runner has no NVIDIA driver.
    from studio.cli_args import args
    args.cpu = True
    import execution
    assert "iam_token" in execution.SENSITIVE_EXTRA_DATA_KEYS
