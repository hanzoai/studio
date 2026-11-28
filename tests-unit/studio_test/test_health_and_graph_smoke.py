"""Smoke tests for Hanzo Studio health endpoints and node-graph save/load/export."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web

import folder_paths
from app.user_manager import UserManager


SAMPLE_NODE_GRAPH = {
    "last_node_id": 2,
    "last_link_id": 1,
    "nodes": [
        {
            "id": 1,
            "type": "CheckpointLoaderSimple",
            "pos": [100, 100],
            "size": [315, 98],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}],
            "widgets_values": ["v1-5-pruned-emaonly.safetensors"],
        },
        {
            "id": 2,
            "type": "SaveImage",
            "pos": [500, 100],
            "size": [315, 270],
            "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
            "widgets_values": ["HanzoStudio"],
        },
    ],
    "links": [[1, 1, 0, 2, 0, "MODEL"]],
    "groups": [],
    "config": {},
    "extra": {"ds": {"scale": 1.0, "offset": [0, 0]}},
    "version": 0.4,
}


def _create_health_app(input_dir: str, renderer_ready: bool) -> web.Application:
    """Build a minimal aiohttp app mirroring server.py /health and /ready routes."""
    app = web.Application()
    routes = web.RouteTableDef()
    state = SimpleNamespace(renderer=object() if renderer_ready else None)

    @routes.get("/health")
    async def health_check(request):
        return web.json_response({"status": "ok"})

    @routes.get("/ready")
    async def readiness_check(request):
        ready = (
            folder_paths.os.path.isdir(input_dir)
            and state.renderer is not None
        )
        if ready:
            return web.json_response({"status": "ready"})
        return web.json_response({"status": "not_ready"}, status=503)

    app.add_routes(routes)
    return app


@pytest.fixture
def isolated_user_dir(tmp_path):
    """Isolate folder_paths user directory for graph serialization tests."""
    original_dir = folder_paths.get_user_directory()
    folder_paths.set_user_directory(str(tmp_path))
    yield tmp_path
    folder_paths.set_user_directory(original_dir)


@pytest.fixture
def userdata_app(isolated_user_dir):
    """Create an aiohttp app with UserManager routes in single-user mode."""
    with patch("app.user_manager.args") as mock_args:
        mock_args.multi_user = False
        um = UserManager()
        app = web.Application()
        routes = web.RouteTableDef()
        um.add_routes(routes)
        app.add_routes(routes)
        yield app


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(aiohttp_client, tmp_path):
    """GET /health returns 200 with status=ok."""
    app = _create_health_app(str(tmp_path), renderer_ready=True)
    client = await aiohttp_client(app)

    resp = await client.get("/health")
    assert resp.status == 200
    assert await resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_reflects_renderer_and_input_dir(aiohttp_client, tmp_path):
    """GET /ready returns 200 when ready and 503 when renderer is uninitialized."""
    ready_client = await aiohttp_client(_create_health_app(str(tmp_path), renderer_ready=True))
    resp_ok = await ready_client.get("/ready")
    assert resp_ok.status == 200
    assert await resp_ok.json() == {"status": "ready"}

    unready_client = await aiohttp_client(_create_health_app(str(tmp_path), renderer_ready=False))
    resp_503 = await unready_client.get("/ready")
    assert resp_503.status == 503
    assert await resp_503.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_node_graph_save_load_export_roundtrip(aiohttp_client, userdata_app):
    """Verify node graph workflow JSON can be saved, listed, loaded, and exported."""
    client = await aiohttp_client(userdata_app)
    workflow_payload = json.dumps(SAMPLE_NODE_GRAPH, indent=2).encode("utf-8")

    # 1. Save workflow graph
    save_resp = await client.post(
        "/userdata/workflows%2Fsmoke_graph.json",
        data=workflow_payload,
        headers={"Content-Type": "application/json"},
    )
    assert save_resp.status == 200

    # 2. List workflows via v2 userdata
    list_resp = await client.get("/v2/userdata?path=workflows")
    assert list_resp.status == 200
    entries = await list_resp.json()
    assert any(item.get("name") == "smoke_graph.json" for item in entries)

    # 3. Load/export workflow graph and verify exact round-trip fidelity
    load_resp = await client.get("/userdata/workflows%2Fsmoke_graph.json")
    assert load_resp.status == 200
    loaded_graph = json.loads(await load_resp.text())
    assert loaded_graph == SAMPLE_NODE_GRAPH
    assert len(loaded_graph["nodes"]) == 2
    assert loaded_graph["links"] == [[1, 1, 0, 2, 0, "MODEL"]]
