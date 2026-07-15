import json
from mcg.tools.platform_adapt import (
    normalize_tools,
    extract_slash_commands,
    extract_skill_name,
    adapt_args_for_tool,
    resolve_forced_call,
)
from mcg.tools.repair import force_tool_call
from mcg.tools.loop import parse_tool_calls_from_text
from mcg.compat.openai_chat import OpenAIChatRequest, to_canonical


def _tools():
    raw = [
        {"type": "function", "function": {
            "name": "use_skill", "description": "load skill",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        }},
        {"type": "function", "function": {
            "name": "Write", "description": "write file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        }},
        {"type": "function", "function": {
            "name": "tavily_search", "description": "search",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "获取时间信息", "description": "time",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"name": "Bash", "description": "shell", "input_schema": {
            "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]
        }},
        {"function_declarations": [{"name": "Read", "description": "read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}
        }}]},
    ]
    return normalize_tools(raw), raw


def test_normalize_multi_platform():
    tools, _ = _tools()
    names = {t.name for t in tools}
    assert names == {"use_skill", "Write", "tavily_search", "获取时间信息", "Bash", "Read"}


def test_slash_vs_path():
    assert extract_slash_commands("/story-setup") == ["story-setup"]
    assert extract_skill_name("/story-setup") == "story-setup"
    assert extract_slash_commands("写入 /tmp/a.txt") == []
    assert extract_skill_name("写入 /tmp/a.txt") is None


def test_force_skill_router():
    tools, _ = _tools()
    c = force_tool_call(tools, user_text="/story-setup")[0]["function"]
    assert c["name"] == "use_skill"
    assert json.loads(c["arguments"])["name"] == "story-setup"
    c = force_tool_call(tools, user_text="调用 skill story-setup")[0]["function"]
    assert c["name"] == "use_skill"


def test_force_write_search_time_bash():
    tools, _ = _tools()
    c = force_tool_call(tools, user_text="用 Write 把 hi 写入 /tmp/a.txt")[0]["function"]
    assert c["name"] == "Write"
    args = json.loads(c["arguments"])
    assert args["path"] == "/tmp/a.txt"
    assert args["content"] == "hi"
    c = force_tool_call(tools, user_text="调用我的tavily搜索工具查今天新闻")[0]["function"]
    assert c["name"] == "tavily_search"
    c = force_tool_call(tools, user_text="现在几点")[0]["function"]
    assert c["name"] == "获取时间信息"
    c = force_tool_call(tools, user_text="用 Bash 执行 echo hi")[0]["function"]
    assert c["name"] == "Bash"
    assert "echo hi" in json.loads(c["arguments"])["command"]


def test_parse_use_skill_fence():
    tools, _ = _tools()
    p = parse_tool_calls_from_text('```use_skill\n{"name":"story-setup"}\n```', tools)
    assert p.tool_calls[0]["function"]["name"] == "use_skill"


def test_adapt_args_synonyms():
    tools, _ = _tools()
    w = next(t for t in tools if t.name == "Write")
    assert adapt_args_for_tool(w, {"file_path": "/tmp/x", "text": "hi"}) == {
        "path": "/tmp/x",
        "content": "hi",
    }


def test_openai_to_canonical_mixed():
    _, raw = _tools()
    req = OpenAIChatRequest(model="x", messages=[{"role": "user", "content": "hi"}], tools=raw)
    names = [t.name for t in to_canonical(req).tools]
    assert "use_skill" in names and "Bash" in names and "Read" in names
