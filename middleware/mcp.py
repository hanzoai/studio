"""MCP-over-HTTP for Hanzo Studio — the studio render pipeline as a Model Context
Protocol server so the cloud chat `create` capability can drive it.

POST /v1/mcp speaks JSON-RPC 2.0 (the MCP wire format) and exposes the SAME render
dispatch the /v1/library/{fix,compose} routes use — ``studio_home.dispatch_fix`` /
``studio_home.dispatch_compose`` — as MCP tools. So a fix/compose an LLM issues
behaves identically to one the Studio UI issues: one dispatch path, no second
pipeline. Auth and org ride the existing IAM middleware exactly like every other
/v1/* route (org = ``studio_home._org_of(request)``); nothing is registered here
that the global auth layer does not already gate.

Methods (JSON-RPC 2.0 envelope: {jsonrpc,id,result|error}):
    initialize   -> {protocolVersion, serverInfo, capabilities:{tools:{}}}
    tools/list   -> the fix / compose / ask tools + JSON schemas
    tools/call   -> dispatch fix|compose|ask and return MCP tool-result content
                    (the render prompt_id / output_prefix). A DispatchError
                    becomes an isError tool-result, never a 500. An unknown
                    METHOD is a JSON-RPC method-not-found error.

── Registration: the ONE recipe ──────────────────────────────────────────────
An operator/admin registers THIS studio MCP server per-org, once, through the
cloud tool registry — the studio public URL with the /v1/mcp path:

    POST https://api.hanzo.ai/v1/tools/servers
    { "name": "studio",
      "url":  "https://studio.hanzo.ai/v1/mcp",
      "org":  "<org>" }

Cloud then calls tools/list here and exposes the returned tools to /v1/chat's
`create` capability as SourceMCP tools. There is no per-tool wiring in cloud: the
tool set is whatever tools/list returns, so adding a studio tool is a one-line
change to _TOOLS here — it flows to chat with no cloud deploy.
"""
from __future__ import annotations

import json

from aiohttp import web

from middleware import studio_home
from studio_version import __version__

# The MCP revision this server implements. Reported in `initialize`.
_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 error codes (only the ones we emit).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601

# The studio render tools. `inputSchema` is the JSON Schema cloud advertises to
# the model; the dispatch functions validate again (instruction length, asset
# existence), so the schema is a hint, not the trust boundary.
_TOOLS = [
    {
        "name": "fix",
        "description": (
            "Edit one library asset with an instruction (Qwen image-edit). "
            "Returns the render prompt_id and output_prefix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "library-relative path of the asset to edit"},
                "instruction": {"type": "string", "description": "the edit to apply (3-500 chars)"},
            },
            "required": ["path", "instruction"],
        },
    },
    {
        "name": "compose",
        "description": (
            "Combine 2-5 library assets into a new output per an instruction. "
            "Returns the render prompt_id and output_prefix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 5,
                    "description": "2-5 library-relative asset paths to combine",
                },
                "instruction": {"type": "string", "description": "how to combine them (3-500 chars)"},
            },
            "required": ["paths", "instruction"],
        },
    },
    {
        "name": "ask",
        "description": (
            "Clarifier no-op: echo a question back to the user when the request is "
            "ambiguous. Produces no render."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "the clarifying question to ask"},
            },
            "required": ["question"],
        },
    },
]


def _result(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload: dict, is_error: bool = False) -> dict:
    """An MCP tool result: text content parts + optional isError flag. The text is
    the JSON dispatch payload (prompt_id / output_prefix) or the error message."""
    r = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        r["isError"] = True
    return r


async def _call_tool(request: web.Request, server, name: str, arguments: dict):
    """Dispatch one tool call to the shared render path. Returns the MCP
    tool-result, or None for an unknown tool name (caller maps that to isError)."""
    org = studio_home._org_of(request)
    if name == "fix":
        out = await studio_home.dispatch_fix(
            request, server, org, arguments.get("path", ""), arguments.get("instruction", ""))
        return _content(out)
    if name == "compose":
        out = await studio_home.dispatch_compose(
            request, server, org, arguments.get("paths") or [], arguments.get("instruction", ""))
        return _content(out)
    if name == "ask":
        return _content({"ok": True, "question": str(arguments.get("question", "")).strip()})
    return None


async def _handle(request: web.Request, server, msg) -> dict | None:
    """Process one JSON-RPC 2.0 message. Returns the response envelope, or None
    for a notification (a message with no ``id`` — no response is sent)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, _INVALID_REQUEST, "invalid JSON-RPC 2.0 request")
    is_notification = "id" not in msg
    id_ = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": _PROTOCOL_VERSION,
            "serverInfo": {"name": "hanzo-studio", "version": __version__},
            "capabilities": {"tools": {"listChanged": False}},
        }
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = await _call_tool(request, server, name, arguments)
        except studio_home.DispatchError as e:
            result = _content({"error": str(e)}, is_error=True)
        if result is None:
            result = _content({"error": f"unknown tool: {name}"}, is_error=True)
    else:
        return None if is_notification else _error(id_, _METHOD_NOT_FOUND, f"unknown method: {method}")

    return None if is_notification else _result(id_, result)


def add_mcp_routes(routes: web.RouteTableDef, server) -> None:
    """Register POST /v1/mcp — the studio MCP-over-HTTP endpoint."""

    @routes.post("/v1/mcp")
    async def mcp_endpoint(request: web.Request):
        try:
            msg = await request.json()
        except Exception:
            return web.json_response(_error(None, _PARSE_ERROR, "invalid JSON"), status=400)
        resp = await _handle(request, server, msg)
        if resp is None:  # notification: acknowledge with no body
            return web.Response(status=202)
        return web.json_response(resp)
