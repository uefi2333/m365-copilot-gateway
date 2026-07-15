import json
from collections.abc import AsyncIterator

import pytest

from mcg.compat.openai_chat import stream_openai_chunks, _openai_tool_call_deltas
from mcg.pool.sessions import SessionStore
from mcg.tools.loop import parse_tool_calls_from_text
from mcg.compat.canonical import CanonicalTool


@pytest.mark.asyncio
async def test_stream_emits_openai_tool_call_shape():
    async def text() -> AsyncIterator[str]:
        yield "I will call a tool.\n"
        yield "```bash\necho hi\n```"

    tools = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})},
        }
    ]
    # simulate holder filled after text — stream_openai_chunks reads holder after iter
    holder = []

    async def wrapped() -> AsyncIterator[str]:
        async for p in text():
            yield p
        holder.extend(tools)

    chunks: list[dict] = []
    async for line in stream_openai_chunks(
        model="m365-copilot",
        text_iter=wrapped(),
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
    # at least one delta with tool_calls index
    tool_deltas = [
        c["choices"][0]["delta"].get("tool_calls")
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
    ]
    assert tool_deltas
    first = tool_deltas[0][0]
    assert first["index"] == 0
    assert first.get("id") == "call_abc" or first.get("function", {}).get("name") == "bash"


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


def test_parse_fence_bash():
    tools = [CanonicalTool(name="bash", description="run", parameters={})]
    p = parse_tool_calls_from_text("```bash\npwd\n```", tools)
    assert p.tool_calls
    assert p.tool_calls[0]["function"]["name"] == "bash"


def test_session_sticky():
    s = SessionStore(ttl_sec=60)
    a = s.get_or_create("u:me", account_id="oid1")
    b = s.get_or_create("u:me", account_id="oid1")
    assert a.conversation_id == b.conversation_id
    c = s.get_or_create("u:me", account_id="oid1", force_new=True)
    assert c.conversation_id != a.conversation_id
