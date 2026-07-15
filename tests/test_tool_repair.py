from mcg.compat.canonical import CanonicalTool
from mcg.tools.loop import parse_tool_calls_from_text
from mcg.tools.repair import looks_like_failed_tool_turn, build_repair_prompt


def test_parse_single_name_arguments_object():
    tools = [CanonicalTool(name="astrbot_file_read_tool", description="", parameters={})]
    raw = 'Here: {"name":"astrbot_file_read_tool","arguments":{"path":"/tmp/a"}}'
    p = parse_tool_calls_from_text(raw, tools)
    assert p.tool_calls
    assert p.tool_calls[0]["function"]["name"] == "astrbot_file_read_tool"


def test_parse_xml_tool_call():
    tools = [CanonicalTool(name="get_weather", description="", parameters={})]
    raw = '<tool_call name="get_weather">{"city":"Tokyo"}</tool_call>'
    p = parse_tool_calls_from_text(raw, tools)
    assert p.tool_calls[0]["function"]["name"] == "get_weather"


def test_narrate_detected():
    tools = [CanonicalTool(name="astrbot_file_read_tool", description="", parameters={})]
    text = "Reading relevant skills\nI need to review the available skills... only docx pdfs"
    assert looks_like_failed_tool_turn(text, tools, True)


def test_repair_prompt_lists_names():
    tools = [CanonicalTool(name="astrbot_file_read_tool", description="", parameters={})]
    s = build_repair_prompt(tools, "fake")
    assert "astrbot_file_read_tool" in s
