from __future__ import annotations

import asyncio
import base64
import json

from mcg.compat.anthropic_messages import (
    AnthropicMessagesRequest,
    final_anthropic_response,
    to_canonical as anth_to_canonical,
)
from mcg.compat.openai_chat import OpenAIChatRequest, to_canonical
from mcg.models_probe import catalog_snapshot, entries_to_openai
from mcg.multimodal.adapter import extract_from_content, render_multimodal_prompt, substrate_message_extras
from mcg.tools.local_exec import LocalToolRunner
from mcg.tools.loop import parse_tool_calls_from_text
from mcg.compat.canonical import CanonicalTool


def test_multimodal_data_url_image():
    # 1x1 png
    png = base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    ).decode()
    content = [
        {"type": "text", "text": "what color?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
    ]
    bundle = extract_from_content(content)
    assert bundle.text == "what color?"
    assert len(bundle.parts) == 1
    assert bundle.parts[0].kind == "image"
    assert bundle.parts[0].bytes_len > 0
    prompt = render_multimodal_prompt(bundle.text, bundle.parts)
    assert "multimodal attachments" in prompt
    extras = substrate_message_extras(bundle.parts)
    assert "imageBase64" in extras or "imageUrl" in extras


def test_openai_canonical_keeps_media():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).decode()
    req = OpenAIChatRequest(
        model="m365-copilot",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png}"},
                    },
                ],
            }
        ],
    )
    canon = to_canonical(req)
    assert "multimodal attachments" in canon.messages[0].content
    assert canon.extra.get("multimodal_parts")


def test_local_runner_echo():
    runner = LocalToolRunner(enabled=True, timeout_sec=5)
    tc = {
        "id": "call_test1",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": "echo P0-LOCAL-OK"}),
        },
    }
    res = asyncio.get_event_loop().run_until_complete(runner.run_one(tc))
    assert res.ok
    assert "P0-LOCAL-OK" in res.content
    msgs = runner.as_tool_messages([res])
    assert msgs[0]["role"] == "tool"


def test_local_runner_deny():
    runner = LocalToolRunner(enabled=True)
    tc = {
        "id": "call_x",
        "function": {"name": "bash", "arguments": '{"command": "rm -rf /"}'},
    }
    res = asyncio.get_event_loop().run_until_complete(runner.run_one(tc))
    assert not res.ok
    assert "denied" in res.content


def test_anthropic_to_canonical_tools():
    req = AnthropicMessagesRequest(
        model="claude-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "name": "bash",
                "description": "run",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ],
        system="be brief",
    )
    canon = anth_to_canonical(req)
    assert canon.messages[0].role == "system"
    assert canon.tools[0].name == "bash"
    out = final_anthropic_response(
        model="claude-sonnet",
        content="hello",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"ls"}'},
            }
        ],
    )
    assert out["stop_reason"] == "tool_use"
    assert out["content"][1]["type"] == "tool_use"


def test_models_probe_catalog():
    entries = catalog_snapshot()
    assert any(e.id == "m365-copilot" for e in entries)
    data = entries_to_openai(entries)
    assert data[0]["object"] == "model"
    assert "tone" in data[0]["metadata"]


def test_parse_still_works_with_bash_fence():
    tools = [CanonicalTool(name="bash", description="shell", parameters={})]
    text = 'I will run:\n```bash\necho hi\n```\n'
    parsed = parse_tool_calls_from_text(text, tools)
    assert parsed.tool_calls
    args = json.loads(parsed.tool_calls[0]["function"]["arguments"])
    assert args["command"] == "echo hi"
