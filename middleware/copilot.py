"""
Chat copilot backend for Hanzo Studio.

POST /v1/copilot/chat turns a natural-language message about the workflow into
graph operations the FRONTEND applies against app.graph (LiteGraph). The backend
only decides ops — it never touches the graph itself.

One tool-calling round trip:

    request   {messages, graph_json, catalog?}
      -> proxy to an OpenAI-compatible chat endpoint with a system prompt that
         describes the graph + the ops tool
      -> {reply, ops}   (add_node ops validated against the live node catalog
                          here so the model can't invent node types)

Env (all optional):
    STUDIO_COPILOT_URL    chat-completions URL. Default: the local engine at
                          http://127.0.0.1:1234/v1/chat/completions if that port
                          is open, else https://api.hanzo.ai/v1/chat/completions.
    STUDIO_COPILOT_MODEL  model id (default "enso" — the Hanzo flagship assistant).
    STUDIO_COPILOT_TOKEN  bearer for the gateway — the ONLY server key for this lane.
                          Falls back to the caller's own token when the gateway can
                          verify it (gateway_auth). A COMMERCE service token is NOT an
                          AI credential and is deliberately not consulted.

The op schema is small and explicit — the frontend applier mirrors it exactly:

    {op:"add_node", type, id?, pos?}      id = a label you reuse to reference
                                          this new node in later ops
    {op:"set_widget", node_id, name, value}
    {op:"connect", from_node, from_slot, to_node, to_slot}  slots by name or index
    {op:"move_node", node_id, pos:[x,y]}
    {op:"delete_node", node_id}
    {op:"set_prompt", node_id, text}      sets a node's primary text widget
    {op:"layout"}                         left-to-right columnar tidy
    {op:"queue"}                          run the workflow
"""
from __future__ import annotations

import json
import logging
import os
import socket
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# Ops the frontend applier understands, and their required fields. layout/queue
# take no args. slots default to 0 when omitted.
_OP_REQUIRED = {
    "add_node": ("type",),
    "set_widget": ("node_id", "name"),
    "connect": ("from_node", "to_node"),
    "move_node": ("node_id", "pos"),
    "delete_node": ("node_id",),
    "set_prompt": ("node_id", "text"),
    "layout": (),
    "queue": (),
}

_GATEWAY_URL = "https://api.hanzo.ai/v1/chat/completions"
_LOCAL_URL = "http://127.0.0.1:1234/v1/chat/completions"

# Server-side gateway keys, in precedence order (never in page source). When none is set
# the caller's own token is forwarded ONLY when the gateway can verify it (pk-/asymmetric
# JWT) — see gateway_auth. This is why a browser HS256 session token no longer 502s.
# One credential, one audience: the AI gateway's server key ONLY. STUDIO_COMMERCE_TOKEN
# used to sit here — it is an HS256 *commerce* token, so it "won" this slot and made every
# chat 502 while masking the caller's own gateway-verifiable IAM token.
_SERVER_TOKEN_ENV = ("STUDIO_COPILOT_TOKEN",)

# Single tool: the model calls it to both reply and edit the graph.
_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_graph",
        "description": (
            "Reply to the user and, when they ask to change the workflow, return "
            "the ordered graph operations to apply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "Short natural-language reply describing what you did or answering the question.",
                },
                "ops": {
                    "type": "array",
                    "description": "Ordered graph operations. Empty when no change is needed.",
                    "items": {"type": "object"},
                },
            },
            "required": ["reply", "ops"],
        },
    },
}

_SYSTEM = """You are the Hanzo Studio Copilot. Hanzo Studio is a node-graph image/video generator (a ComfyUI fork). You edit the user's live workflow graph by returning graph operations through the edit_graph tool.

Op schema (return ops in the order they should apply):
  {"op":"add_node","type":"<NodeType>","id":"<label>","pos":[x,y]}   id is a label you invent; reuse it in later ops to reference this new node
  {"op":"set_widget","node_id":<id>,"name":"<widget>","value":<v>}
  {"op":"connect","from_node":<id>,"from_slot":<name|index>,"to_node":<id>,"to_slot":<name|index>}
  {"op":"set_prompt","node_id":<id>,"text":"<prompt text>"}
  {"op":"move_node","node_id":<id>,"pos":[x,y]}
  {"op":"delete_node","node_id":<id>}
  {"op":"layout"}    tidy the whole graph left-to-right
  {"op":"queue"}     run the workflow

Rules:
- Reference existing nodes by their numeric id from CURRENT GRAPH. Reference nodes you add by the id label you gave them in add_node.
- Prefer connecting slots by NAME (e.g. from_slot "IMAGE", to_slot "image"); the client resolves names to indices. Use index 0 if unsure.
- Only use node types that exist. Pick from NODE TYPES; if a type is not listed, do not invent it.
- Keep ops minimal — do exactly what the user asked, nothing more.
- Always call edit_graph. If the user is only chatting/asking, reply with an empty ops array."""


def _default_url() -> str:
    """Local engine if its port is open, else the Hanzo gateway. Probed once at import."""
    try:
        with socket.create_connection(("127.0.0.1", 1234), timeout=0.2):
            return _LOCAL_URL
    except OSError:
        return _GATEWAY_URL


_DEFAULT_URL = _default_url()

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


class CopilotError(Exception):
    """Upstream LLM call failed."""


def _resolve_target(request) -> tuple[str, str, str]:
    """(url, model, bearer_token) for this request.

    The bearer is chosen by the ONE fail-closed gateway allowlist (``gateway_auth``): a
    server-side key first, else the caller's own token ONLY when the gateway can verify it
    (a publishable ``pk-`` key or an asymmetric/JWKS JWT). A symmetric HS256 hanzo.id
    session token is NEVER forwarded — the gateway rejects it ("unsupported signing method:
    HS256"), which was the 502 that made Chat show a raw error. ``token`` is the bare value
    (no ``Bearer `` prefix) since ``_post_chat`` adds it; "" when none is usable."""
    from middleware import gateway_auth
    url = os.environ.get("STUDIO_COPILOT_URL", "").strip() or _DEFAULT_URL
    model = os.environ.get("STUDIO_COPILOT_MODEL", "").strip() or "enso"
    bearer = gateway_auth.ai_bearer(request, server_env=_SERVER_TOKEN_ENV)
    token = bearer[7:] if bearer[:7].lower() == "bearer " else bearer
    return url, model, token


def validate_ops(ops, valid_types: set) -> tuple[list, list]:
    """Drop malformed ops and add_node ops naming unknown node types.

    Returns (clean_ops, warnings). The frontend guards again, but validating
    node types here is the one place the model is prevented from inventing nodes.
    """
    clean, warnings = [], []
    if not isinstance(ops, list):
        return clean, warnings
    for op in ops:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name not in _OP_REQUIRED:
            warnings.append(f"unknown op {name!r}")
            continue
        missing = [f for f in _OP_REQUIRED[name] if op.get(f) in (None, "")]
        if missing:
            warnings.append(f"{name} missing {', '.join(missing)}")
            continue
        if name == "add_node" and op["type"] not in valid_types:
            warnings.append(f"unknown node type {op['type']!r}")
            continue
        clean.append(op)
    return clean, warnings


def _build_messages(body: dict) -> list:
    graph_json = body.get("graph_json")
    catalog = body.get("catalog") or {}
    history = body.get("messages") or []

    types = catalog.get("types") or []
    detail = catalog.get("nodes") or {}
    ctx = (
        _SYSTEM
        + "\n\nNODE TYPES (you may only add these):\n"
        + ", ".join(sorted(types)[:1200])
        + "\n\nSLOT DETAIL for node types currently in the graph:\n"
        + json.dumps(detail)[:12000]
        + "\n\nCURRENT GRAPH (node id -> {class_type, inputs}):\n"
        + json.dumps(graph_json)[:24000]
    )
    msgs = [{"role": "system", "content": ctx}]
    # Only user/assistant turns from the client; cap history.
    for m in history[-20:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])})
    return msgs


def _parse_completion(data: dict) -> tuple[str, list]:
    """Pull (reply, ops) from an OpenAI-style completion, tolerating either a
    tool_call or a JSON/plain-text content reply."""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "", []

    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name") != "edit_graph":
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        return str(args.get("reply", "")), args.get("ops") or []

    content = msg.get("content") or ""
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = content.strip()

    # Fallback: a bare JSON object embedded in the prose.
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(content[start : end + 1])
            if isinstance(obj, dict) and "ops" in obj:
                return str(obj.get("reply", content)), obj.get("ops") or []
        except json.JSONDecodeError:
            pass
    return content, []


async def _post_chat(url: str, token: str, payload: dict) -> dict:
    """POST an OpenAI-compatible chat completion. Raises CopilotError on failure."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        session = await _get_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise CopilotError(f"LLM {resp.status}: {text[:300]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise CopilotError(f"bad LLM JSON: {e}") from e
    except aiohttp.ClientError as e:
        raise CopilotError(f"LLM unreachable: {e}") from e


async def run_copilot(body: dict, valid_types: set, *, url: str, model: str, token: str) -> dict:
    """One tool-calling round trip. Returns {reply, ops} with ops validated."""
    payload = {
        "model": model,
        "messages": _build_messages(body),
        "tools": [_EDIT_TOOL],
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    data = await _post_chat(url, token, payload)
    reply, ops = _parse_completion(data)
    clean, warnings = validate_ops(ops, valid_types)
    if warnings:
        note = "Skipped: " + "; ".join(warnings)
        reply = f"{reply}\n\n{note}" if reply else note
    return {"reply": reply, "ops": clean}


def add_copilot_routes(routes: web.RouteTableDef, server) -> None:
    """Register POST /v1/copilot/chat."""

    @routes.post("/v1/copilot/chat")
    async def copilot_chat(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict) or not body.get("messages"):
            return web.json_response({"error": "messages is required"}, status=400)

        import nodes  # loaded by the time routes are added; kept out of module import for light tests
        valid_types = set(nodes.NODE_CLASS_MAPPINGS.keys())

        url, model, token = _resolve_target(request)
        try:
            result = await run_copilot(body, valid_types, url=url, model=model, token=token)
        except CopilotError as e:
            logger.warning("copilot: %s", e)
            return web.json_response({"error": str(e), "reply": "", "ops": []}, status=502)
        return web.json_response(result)
