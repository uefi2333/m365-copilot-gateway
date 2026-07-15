from mcg.compat.canonical import CanonicalTool
from mcg.tools.agents import detect_agent, has_deploy_tools, rewrite_tool_call_ids, PROFILES
from mcg.tools.platform_adapt import family_of, normalize_tools


def _t(name, **kw):
    return CanonicalTool(name=name, description="", parameters=kw.get("parameters") or {})


def test_detect_claude_code_path():
    tools = [_t("Bash"), _t("Read"), _t("Write"), _t("Edit"), _t("Skill")]
    p = detect_agent(tools=tools, path="/v1/messages")
    assert p.id == "claude_code"


def test_detect_codex_tools():
    tools = [_t("shell"), _t("apply_patch"), _t("update_plan")]
    p = detect_agent(tools=tools, path="/v1/chat/completions")
    assert p.id == "codex"


def test_detect_cursor():
    tools = [_t("run_terminal_cmd"), _t("read_file"), _t("codebase_search"), _t("edit_file")]
    p = detect_agent(tools=tools)
    assert p.id == "cursor"


def test_detect_phone_lite():
    tools = [_t("search_web"), _t("scrape_web"), _t("get_time_info"), _t("use_skill")]
    p = detect_agent(tools=tools)
    assert p.id == "phone_lite"
    assert p.hop2_passthrough is True
    assert p.hop2_chain is False
    assert not has_deploy_tools(tools)


def test_family_aliases_cc_codex_cursor():
    assert family_of("apply_patch") == "edit"
    assert family_of("run_terminal_cmd") == "shell"
    assert family_of("write_to_file") == "write"
    assert family_of("local_shell") == "shell"
    assert family_of("WebFetch") == "web_search" or family_of("webfetch") == "web_search"


def test_rewrite_ids():
    calls = [{"id": "call_abc123", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]
    out = rewrite_tool_call_ids(calls, PROFILES["claude_code"])
    assert out[0]["id"].startswith("toolu_")


def test_normalize_anthropic_and_gemini():
    raw = [
        {"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
        {"function_declarations": [{"name": "shell", "parameters": {"type": "object"}}]},
    ]
    tools = normalize_tools(raw)
    assert {t.name for t in tools} == {"Bash", "shell"}
