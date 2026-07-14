from mcg.substrate.client import fold_stream_text
from mcg.tools.loop import parse_tool_calls_from_text
from mcg.compat.canonical import CanonicalTool


def test_fold_prefix():
    a, e = fold_stream_text("hel", "hello")
    assert a == "hello" and e == "lo"


def test_fold_shorter():
    a, e = fold_stream_text("hello", "hel")
    assert a == "hello" and e is None


def test_parse_fence_tool():
    tools = [CanonicalTool(name="lookup", description="d", parameters={"type": "object"})]
    text = 'before\n```lookup\n{"q": "x"}\n```\nafter'
    p = parse_tool_calls_from_text(text, tools)
    assert len(p.tool_calls) == 1
    assert p.tool_calls[0]["function"]["name"] == "lookup"
