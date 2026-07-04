"""Tests for the Copilot backend: op validation + the tool-calling round trip.

No network and no node graph — the LLM call (_post_chat) is monkeypatched and
node types are passed in as a set, so this stays as light as the other
middleware tests. Async paths run via asyncio.run to avoid a plugin dependency.
"""
import asyncio
import json

from middleware import copilot

VALID = {"KSampler", "SaveImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "CLIPTextEncode"}


# --------------------------------------------------------------- validate_ops
def test_valid_ops_pass_through():
    ops = [
        {"op": "add_node", "type": "SaveImage", "id": "s1"},
        {"op": "connect", "from_node": 3, "from_slot": "IMAGE", "to_node": "s1", "to_slot": "images"},
        {"op": "set_widget", "node_id": 3, "name": "seed", "value": 42},
        {"op": "layout"},
        {"op": "queue"},
    ]
    clean, warnings = copilot.validate_ops(ops, VALID)
    assert clean == ops
    assert warnings == []


def test_unknown_node_type_dropped():
    clean, warnings = copilot.validate_ops(
        [{"op": "add_node", "type": "TotallyFakeNode"}], VALID
    )
    assert clean == []
    assert any("TotallyFakeNode" in w for w in warnings)


def test_unknown_op_dropped():
    clean, warnings = copilot.validate_ops([{"op": "teleport", "node_id": 1}], VALID)
    assert clean == []
    assert any("unknown op" in w for w in warnings)


def test_missing_required_field_dropped():
    clean, warnings = copilot.validate_ops([{"op": "set_widget", "node_id": 1}], VALID)
    assert clean == []
    assert any("missing name" in w for w in warnings)


def test_set_widget_zero_value_is_kept():
    # value 0 / "" is meaningful and must not be treated as missing.
    ops = [{"op": "set_widget", "node_id": 1, "name": "denoise", "value": 0}]
    clean, warnings = copilot.validate_ops(ops, VALID)
    assert clean == ops and warnings == []


def test_non_list_ops_safe():
    assert copilot.validate_ops(None, VALID) == ([], [])
    assert copilot.validate_ops("nope", VALID) == ([], [])


# ------------------------------------------------------------- _parse_completion
def _tool_completion(reply, ops):
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "edit_graph",
                                "arguments": json.dumps({"reply": reply, "ops": ops}),
                            }
                        }
                    ]
                }
            }
        ]
    }


def test_parse_tool_call():
    reply, ops = copilot._parse_completion(
        _tool_completion("done", [{"op": "queue"}])
    )
    assert reply == "done"
    assert ops == [{"op": "queue"}]


def test_parse_content_json_fallback():
    content = 'Sure! {"reply": "added", "ops": [{"op": "layout"}]}'
    data = {"choices": [{"message": {"content": content}}]}
    reply, ops = copilot._parse_completion(data)
    assert ops == [{"op": "layout"}]


def test_parse_prose_only():
    data = {"choices": [{"message": {"content": "That node upscales images."}}]}
    reply, ops = copilot._parse_completion(data)
    assert reply == "That node upscales images."
    assert ops == []


def test_parse_malformed_completion():
    assert copilot._parse_completion({}) == ("", [])


# ---------------------------------------------------------------- run_copilot
def test_run_copilot_validates_after_model(monkeypatch):
    async def fake_post(url, token, payload):
        # The model asks for a good op and a hallucinated node type.
        return _tool_completion(
            "added an upscale",
            [
                {"op": "add_node", "type": "ImageUpscaleWithModel", "id": "u1"},
                {"op": "add_node", "type": "MadeUpNode"},
            ],
        )

    monkeypatch.setattr(copilot, "_post_chat", fake_post)
    body = {"messages": [{"role": "user", "content": "add a 4x upscale"}], "graph_json": {}}
    result = asyncio.run(
        copilot.run_copilot(body, VALID, url="http://x", model="zen", token="")
    )
    assert result["ops"] == [{"op": "add_node", "type": "ImageUpscaleWithModel", "id": "u1"}]
    assert "MadeUpNode" in result["reply"]
    assert "added an upscale" in result["reply"]


def test_run_copilot_prose_no_ops(monkeypatch):
    async def fake_post(url, token, payload):
        return {"choices": [{"message": {"content": "KSampler denoises latents."}}]}

    monkeypatch.setattr(copilot, "_post_chat", fake_post)
    body = {"messages": [{"role": "user", "content": "what does ksampler do?"}]}
    result = asyncio.run(
        copilot.run_copilot(body, VALID, url="http://x", model="zen", token="")
    )
    assert result["ops"] == []
    assert "KSampler" in result["reply"]


def test_run_copilot_sends_tool_and_system(monkeypatch):
    captured = {}

    async def fake_post(url, token, payload):
        captured.update(payload)
        return _tool_completion("ok", [])

    monkeypatch.setattr(copilot, "_post_chat", fake_post)
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "graph_json": {"1": {"class_type": "KSampler", "inputs": {}}},
        "catalog": {"types": ["KSampler"], "nodes": {}},
    }
    asyncio.run(copilot.run_copilot(body, VALID, url="http://x", model="zen", token=""))
    assert captured["tools"][0]["function"]["name"] == "edit_graph"
    assert captured["messages"][0]["role"] == "system"
    assert "KSampler" in captured["messages"][0]["content"]
    # user turn is forwarded after the system prompt
    assert captured["messages"][-1] == {"role": "user", "content": "hi"}
