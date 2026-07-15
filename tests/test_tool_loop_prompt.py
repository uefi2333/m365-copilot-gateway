import json

from mcg.compat.canonical import CanonicalMessage, CanonicalRequest, CanonicalTool
from mcg.tools.loop import ToolLoop


def test_prompt_includes_assistant_tool_calls_and_tool_results():
    req = CanonicalRequest(
        model="m365-copilot",
        messages=[
            CanonicalMessage(role="user", content="What is the secret code from the shell?"),
            CanonicalMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": "echo MCG-SECRET-42"}),
                        },
                    }
                ],
            ),
            CanonicalMessage(
                role="tool",
                name="bash",
                tool_call_id="call_1",
                content="MCG-SECRET-42\n",
            ),
        ],
        tools=[
            CanonicalTool(
                name="bash",
                description="run",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            )
        ],
    )
    text = req.prompt_text()
    assert "[assistant_tool_calls]" in text
    assert "bash" in text
    assert "MCG-SECRET-42" in text
    assert "tool_call_id=call_1" in text

    loop = ToolLoop()
    aug = loop.augment_prompt(req)
    assert "Tool results" in aug
    assert "Do NOT re-call" in aug
    assert "MCG-SECRET-42" in aug


def test_prompt_plain_user_only():
    req = CanonicalRequest(
        messages=[CanonicalMessage(role="user", content="hi")],
        tools=[],
    )
    assert req.prompt_text() == "hi"
