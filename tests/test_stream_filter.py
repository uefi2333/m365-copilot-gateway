from mcg.tools.stream_filter import StreamToolAccumulator


def test_hold_tool_fence_emit_prose():
    acc = StreamToolAccumulator(["get_current_time"])
    out = []
    for piece in ["Hi ", "there.\n", "```get_current_time\n", '{"timezone":"UTC"}\n', "```\n", "done"]:
        out.extend(acc.feed(piece))
    out.extend(acc.flush())
    joined = "".join(out)
    assert "Hi there." in joined
    assert "get_current_time" not in joined
    assert "done" in joined
    assert "get_current_time" in acc.full


def test_partial_backticks_not_lost():
    acc = StreamToolAccumulator([])
    out = []
    out.extend(acc.feed("a``"))
    out.extend(acc.feed("`b"))
    out.extend(acc.flush())
    assert "".join(out) == "a```b"


def test_non_tool_fence_emitted():
    acc = StreamToolAccumulator(["get_weather"])
    out = []
    for piece in ["```python\nprint(1)\n```"]:
        out.extend(acc.feed(piece))
    out.extend(acc.flush())
    assert "print(1)" in "".join(out)
