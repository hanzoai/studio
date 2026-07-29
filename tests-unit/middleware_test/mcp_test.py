"""Tests for the /v1/mcp MCP-over-HTTP endpoint: initialize, tools/list, the
tools/call dispatch, DispatchError -> isError result, and unknown-method error.

Mirrors session_test.py: a real aiohttp app with a middleware that injects the
IAM user the auth layer would have set, so org resolution and the JSON-RPC
envelopes are the real thing. The render dispatch (studio_home.dispatch_fix /
dispatch_compose) is monkeypatched, so no GPU / self-POST is needed.
"""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from middleware import mcp
from middleware import studio_home

ORG_USER = {"sub": "hanzo/antje", "org_id": "hanzo", "orgs": ["hanzo"]}


def _app(iam_user=ORG_USER):
    app = web.Application()

    @web.middleware
    async def inject(request, handler):
        if iam_user is not None:
            request["iam_user"] = iam_user
        return await handler(request)

    app.middlewares.append(inject)
    routes = web.RouteTableDef()
    mcp.add_mcp_routes(routes, None)
    app.add_routes(routes)
    return app


def _rpc(client, method, params=None, id_=1):
    payload = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/v1/mcp", json=payload)


def _run(coro):
    return asyncio.run(coro)


# --- initialize ----------------------------------------------------------

def test_initialize_reports_server_and_tools_capability():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "initialize")
            assert r.status == 200
            return await r.json()
    body = _run(go())
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "hanzo-studio"
    assert body["result"]["protocolVersion"]
    assert "tools" in body["result"]["capabilities"]


# --- tools/list ----------------------------------------------------------

def test_tools_list_returns_the_render_tools():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/list")
            return await r.json()
    body = _run(go())
    tools = body["result"]["tools"]
    assert {t["name"] for t in tools} == {"fix", "compose", "rerun", "status", "ask"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
    compose = next(t for t in tools if t["name"] == "compose")
    assert compose["inputSchema"]["properties"]["paths"]["minItems"] == 2
    assert compose["inputSchema"]["properties"]["paths"]["maxItems"] == 5


# --- tools/call ----------------------------------------------------------

def test_tools_call_fix_dispatches(monkeypatch):
    seen = {}

    async def fake_fix(request, server, org, path, instruction, **kw):
        seen.update(org=org, path=path, instruction=instruction)
        return {"ok": True, "prompt_id": "p1", "output_prefix": "fixes/x"}

    monkeypatch.setattr(studio_home, "dispatch_fix", fake_fix)

    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/call", {
                "name": "fix",
                "arguments": {"path": "a/b.png", "instruction": "make it red"},
            })
            assert r.status == 200
            return await r.json()
    body = _run(go())
    assert seen == {"org": "hanzo", "path": "a/b.png", "instruction": "make it red"}
    result = body["result"]
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["prompt_id"] == "p1"
    assert payload["output_prefix"] == "fixes/x"


def test_tools_call_compose_dispatches(monkeypatch):
    seen = {}

    async def fake_compose(request, server, org, paths, instruction, **kw):
        seen.update(org=org, paths=paths, instruction=instruction)
        return {"ok": True, "prompt_id": "p2", "output_prefix": "composes/y", "refs": len(paths)}

    monkeypatch.setattr(studio_home, "dispatch_compose", fake_compose)

    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/call", {
                "name": "compose",
                "arguments": {"paths": ["a.png", "b.png"], "instruction": "merge them"},
            })
            return await r.json()
    body = _run(go())
    assert seen["paths"] == ["a.png", "b.png"]
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["prompt_id"] == "p2"
    assert payload["refs"] == 2


def test_tools_call_ask_echoes_question():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/call", {
                "name": "ask", "arguments": {"question": "which asset?"},
            })
            return await r.json()
    body = _run(go())
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["question"] == "which asset?"
    assert body["result"].get("isError") is not True


def test_dispatch_error_becomes_iserror_result(monkeypatch):
    async def bad_fix(request, server, org, path, instruction, **kw):
        raise studio_home.DispatchError("unknown asset: nope.png")

    monkeypatch.setattr(studio_home, "dispatch_fix", bad_fix)

    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/call", {
                "name": "fix", "arguments": {"path": "nope.png", "instruction": "x"},
            })
            assert r.status == 200          # never a 500
            return await r.json()
    body = _run(go())
    assert "error" not in body              # not a JSON-RPC error
    assert body["result"]["isError"] is True
    assert "unknown asset" in body["result"]["content"][0]["text"]


def test_unknown_tool_is_iserror_result():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/call", {"name": "frobnicate", "arguments": {}})
            return await r.json()
    body = _run(go())
    assert body["result"]["isError"] is True


# --- protocol edges ------------------------------------------------------

def test_unknown_method_is_jsonrpc_error():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await _rpc(c, "tools/frobnicate")
            assert r.status == 200
            return await r.json()
    body = _run(go())
    assert "result" not in body
    assert body["error"]["code"] == -32601
    assert body["id"] == 1


def test_notification_gets_no_body():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.post("/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            return r.status, await r.text()
    status, text = _run(go())
    assert status == 202
    assert text == ""


def test_bad_json_is_parse_error():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.post("/v1/mcp", data="not json", headers={"Content-Type": "application/json"})
            return r.status, await r.json()
    status, body = _run(go())
    assert status == 400
    assert body["error"]["code"] == -32700
