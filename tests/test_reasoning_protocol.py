import json
from collections.abc import AsyncIterator

import pytest

from mcg.compat.openai_chat import final_openai_response, stream_openai_chunks
from mcg.compat.reasoning import split_explicit_reasoning


def test_reasoning_only_explicit_marker_is_split():
    out = split_explicit_reasoning("<thinking>plan</thinking>answer")
    assert out.reasoning == "plan"
    assert out.content == "answer"


def test_plain_thinking_word_is_not_reasoning():
    out = split_explicit_reasoning("Thinking about this, the answer is 42.")
    assert out.reasoning == ""
    assert out.content == "Thinking about this, the answer is 42."


def test_final_response_has_openai_reasoning_field():
    payload = final_openai_response(
        model="m365-copilot", content="done", reasoning_content="plan"
    )
    msg = payload["choices"][0]["message"]
    assert msg["reasoning_content"] == "plan"
    assert msg["content"] == "done"


@pytest.mark.asyncio
async def test_stream_reasoning_is_separate_delta():
    async def text() -> AsyncIterator[str]:
        yield "answer"

    async def reasoning() -> AsyncIterator[str]:
        yield "plan"

    rows = []
    async for line in stream_openai_chunks(
        model="m365-copilot", text_iter=text(), reasoning_iter=reasoning()
    ):
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            rows.append(json.loads(line[6:]))
    deltas = [r["choices"][0]["delta"] for r in rows]
    assert any(d.get("reasoning_content") == "plan" for d in deltas)
    assert any(d.get("content") == "answer" for d in deltas)
    assert not any("plan" in (d.get("content") or "") for d in deltas)
