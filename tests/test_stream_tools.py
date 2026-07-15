import json
from collections.abc import AsyncIterator

import pytest

from mcg.compat.canonical import CanonicalTool
from mcg.compat.openai_chat import _openai_tool_call_deltas, stream_openai_chunks
from mcg.pool.sessions import SessionStore
from mcg.tools.loop import parse_tool_calls_from_text


@pytest.mark.asyncio
async def test_stream_emits_openai_tool_call_shape():
    async def text() -> AsyncIterator[str]:
        # cleaned content only (route strips fence before streaming when tools)
        yield "I will run it."

    tools = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})},
        }
    ]
    holder = list(tools)

    chunks: list[dict] = []
    async for line in stream_openai_chunks(
        model="m365-copilot",
        text_iter=text(),
        tool_calls_holder=holder,
        conversation_id="conv-1",
    ):
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        payload = json.loads(line[len("data: ") :].strip())
        chunks.append(payload)
        assert payload.get("conversation_id") == "conv-1"

    finishes = [c["choices"][0]["finish_reason"] for c in chunks]
    assert "tool_calls" in finishes
    tool_deltas = [
        c["choices"][0]["delta"].get("tool_calls")
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
    ]
    assert tool_deltas
    first = tool_deltas[0][0]
    assert first["index"] == 0
    assert first.get("id") == "call_abc" or first.get("function", {}).get("name") == "bash"

    contents = [
        c["choices"][0]["delta"].get("content")
        for c in chunks
        if c["choices"][0]["delta"].get("content")
    ]
    assert any("I will run it" in (c or "") for c in contents)
    assert not any("```" in (c or "") for c in contents)


def test_tool_delta_split():
    frags = _openai_tool_call_deltas(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"ls"}'},
            }
        ]
    )
    assert frags[0]["function"]["name"] == "bash"
    assert frags[0]["function"]["arguments"] == ""
    assert frags[1]["function"]["arguments"] == '{"command":"ls"}'


def test_parse_fence_bash_uses_command():
    tools = [CanonicalTool(name="bash", description="run", parameters={})]
    p = parse_tool_calls_from_text("```bash\necho hello-mcg\n```", tools)
    assert p.tool_calls
    assert p.tool_calls[0]["function"]["name"] == "bash"
    args = json.loads(p.tool_calls[0]["function"]["arguments"])
    assert args == {"command": "echo hello-mcg"}
    assert p.text == ""


def test_parse_fence_named_bash_json_input_normalized():
    tools = [CanonicalTool(name="bash", description="run", parameters={})]
    p = parse_tool_calls_from_text('```bash\n{"input": "pwd"}\n```', tools)
    args = json.loads(p.tool_calls[0]["function"]["arguments"])
    assert args["command"] == "pwd"


def test_parse_json_tool_calls_weather():
    tools = [CanonicalTool(name="get_weather", description="", parameters={})]
    raw = '{"tool_calls":[{"name":"get_weather","arguments":{"city":"Tokyo"}}]}'
    p = parse_tool_calls_from_text(raw, tools)
    assert len(p.tool_calls) == 1
    args = json.loads(p.tool_calls[0]["function"]["arguments"])
    assert args["city"] == "Tokyo"
    assert p.text == ""


def test_session_sticky():
    s = SessionStore(ttl_sec=60)
    a = s.get_or_create("u:me", account_id="oid1")
    b = s.get_or_create("u:me", account_id="oid1")
    assert a.conversation_id == b.conversation_id
    c = s.get_or_create("u:me", account_id="oid1", force_new=True)
    assert c.conversation_id != a.conversation_id
