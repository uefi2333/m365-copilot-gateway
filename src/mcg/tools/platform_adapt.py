"""Multi-platform tool schema + call adaptation.

Normalizes inbound tool definitions and helps pick/emit calls that match
what real clients actually register:

  - OpenAI functions
  - Anthropic tools (input_schema)
  - Gemini function_declarations
  - Claude-Code / Cursor / 国产客户端 skill routers (use_skill / Skill)
  - Common file/shell aliases (Write, Bash, Read, ...)
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from mcg.compat.canonical import CanonicalTool

SKILL_ROUTER_NAMES = frozenset(
    {
        "use_skill",
        "useskill",
        "skill",
        "run_skill",
        "invoke_skill",
        "load_skill",
        "Skill",
        "UseSkill",
        "RunSkill",
        "LoadSkill",
        "InvokeSkill",
        # some CC forks
        "skill_tool",
        "runskill",
    }
)

_FAMILY_ALIASES: dict[str, str] = {
    "bash": "shell",
    "shell": "shell",
    "sh": "shell",
    "zsh": "shell",
    "cmd": "shell",
    "powershell": "shell",
    "ps1": "shell",
    "run_terminal_cmd": "shell",
    "run_terminal_command": "shell",
    "execute_command": "shell",
    "run_command": "shell",
    "terminal": "shell",
    "local_shell": "shell",
    "write": "write",
    "write_file": "write",
    "writefile": "write",
    "create_file": "write",
    "createfile": "write",
    "save_file": "write",
    "file_write": "write",
    "astrbot_file_write_tool": "write",
    "read": "read",
    "read_file": "read",
    "readfile": "read",
    "open_file": "read",
    "file_read": "read",
    "astrbot_file_read_tool": "read",
    "edit": "edit",
    "edit_file": "edit",
    "strreplace": "edit",
    "str_replace": "edit",
    "apply_patch": "edit",
    "search_replace": "edit",
    "astrbot_file_edit_tool": "edit",
    "web_search": "web_search",
    "websearch": "web_search",
    "tavily_search": "web_search",
    "tavily": "web_search",
    "tavily_search_tool": "web_search",
    "google_search": "web_search",
    "bing_search": "web_search",
    "search_web": "web_search",
    "internet_search": "web_search",
    "web_search_tavily": "web_search",
    "time": "time",
    "get_time": "time",
    "current_time": "time",
    "datetime": "time",
    "get_current_time": "time",
    "获取时间信息": "time",

    # Claude Code
    "glob": "read",
    "grep": "read",
    "webfetch": "web_search",
    "web_fetch": "web_search",
    "todowrite": "write",
    "todo_write": "write",
    "notebookedit": "edit",
    "notebook_edit": "edit",
    # Codex
    "apply_patch": "edit",
    "applypatch": "edit",
    "update_plan": "write",
    "local_shell": "shell",
    "view_image": "read",
    "list_dir": "read",
    # Cursor
    "codebase_search": "read",
    "read_file": "read",
    "run_terminal_cmd": "shell",
    "edit_file": "edit",
    "grep_search": "read",
    "file_search": "read",
    "delete_file": "edit",
    # Cline / Roo
    "write_to_file": "write",
    "execute_command": "shell",
    "replace_in_file": "edit",
    "search_files": "read",
    "list_files": "read",
    "attempt_completion": "write",
    # Continue
    "builtin_read_file": "read",
    "builtin_edit_file": "edit",
    "builtin_run_terminal_command": "shell",
    "builtin_grep_search": "read",
    "run_terminal_command": "shell",

    "use_skill": "skill_router",
    "skill": "skill_router",
    "run_skill": "skill_router",
    "invoke_skill": "skill_router",
    "load_skill": "skill_router",
}

_FAMILY_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "shell": ("command", "cmd", "input", "script", "code"),
    "write": ("path", "file_path", "file", "filename", "content", "text", "data"),
    "read": ("path", "file_path", "file", "filename"),
    "edit": ("path", "file_path", "old", "old_string", "new", "new_string", "content"),
    "web_search": ("query", "q", "search", "input", "prompt", "text", "keyword"),
    "time": (),
    "skill_router": ("name", "skill", "skill_name", "id", "skill_id"),
}


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())


def family_of(tool_name: str) -> str | None:
    n = (tool_name or "").strip()
    if not n:
        return None
    low = n.lower()
    if low in _FAMILY_ALIASES:
        return _FAMILY_ALIASES[low]
    nn = _norm_name(n)
    if nn in _FAMILY_ALIASES:
        return _FAMILY_ALIASES[nn]
    for key, fam in _FAMILY_ALIASES.items():
        if key and (key in low or key in nn):
            return fam
    if "skill" in low:
        return "skill_router"
    if any(k in low for k in ("search", "tavily", "搜")):
        return "web_search"
    if any(k in low for k in ("time", "date", "时间", "日期")):
        return "time"
    return None


def is_skill_router(tool: CanonicalTool | str) -> bool:
    name = tool if isinstance(tool, str) else tool.name
    if not name:
        return False
    if name in SKILL_ROUTER_NAMES:
        return True
    if name.lower() in {s.lower() for s in SKILL_ROUTER_NAMES}:
        return True
    return family_of(name) == "skill_router"


def find_skill_router(tools: list[CanonicalTool]) -> CanonicalTool | None:
    for t in tools:
        if is_skill_router(t):
            return t
    return None


def normalize_tool_dict(raw: dict[str, Any]) -> CanonicalTool | None:
    if not isinstance(raw, dict):
        return None

    if "function_declarations" in raw and isinstance(raw["function_declarations"], list):
        if not raw["function_declarations"]:
            return None
        return normalize_tool_dict(raw["function_declarations"][0])

    if raw.get("type") == "function" and isinstance(raw.get("function"), dict):
        fn = raw["function"]
        name = fn.get("name")
        if not name:
            return None
        return CanonicalTool(
            name=str(name),
            description=str(fn.get("description") or ""),
            parameters=fn.get("parameters") or fn.get("input_schema") or {},
        )

    if isinstance(raw.get("function"), dict) and raw["function"].get("name"):
        fn = raw["function"]
        return CanonicalTool(
            name=str(fn["name"]),
            description=str(fn.get("description") or ""),
            parameters=fn.get("parameters") or {},
        )

    name = raw.get("name") or raw.get("tool_name") or raw.get("function_name")
    if not name:
        return None
    params = (
        raw.get("input_schema")
        or raw.get("parameters")
        or raw.get("schema")
        or raw.get("arguments")
        or {}
    )
    if not isinstance(params, dict):
        params = {}
    return CanonicalTool(
        name=str(name),
        description=str(raw.get("description") or raw.get("desc") or ""),
        parameters=params,
    )


def normalize_tools(raw_tools: list[Any] | None) -> list[CanonicalTool]:
    out: list[CanonicalTool] = []
    seen: set[str] = set()
    for t in raw_tools or []:
        if isinstance(t, CanonicalTool):
            ct = t
        elif isinstance(t, dict):
            if "function_declarations" in t and isinstance(t["function_declarations"], list):
                for fd in t["function_declarations"]:
                    ct = normalize_tool_dict(fd if isinstance(fd, dict) else {})
                    if ct and ct.name not in seen:
                        seen.add(ct.name)
                        out.append(ct)
                continue
            ct = normalize_tool_dict(t)
        else:
            continue
        if not ct or not ct.name or ct.name in seen:
            continue
        seen.add(ct.name)
        out.append(ct)
    return out


_PATH_ROOTS = frozenset(
    {"tmp", "home", "usr", "var", "etc", "opt", "data", "mnt", "root", "users"}
)


def extract_slash_commands(user_text: str) -> list[str]:
    """Extract /skill-style commands; ignore absolute paths like /tmp/a.txt."""
    blob = user_text or ""
    m0 = re.fullmatch(r"\s*/([A-Za-z][A-Za-z0-9_\-]{1,63})\s*", blob)
    if m0:
        return [m0.group(1)]

    found: list[str] = []
    for m in re.finditer(r"(?:^|\s)/([A-Za-z][A-Za-z0-9_\-]{1,63})", blob):
        tok = m.group(1)
        end = m.end(1)
        if end < len(blob) and blob[end] in "/\\":
            continue
        if tok.lower() in _PATH_ROOTS:
            continue
        if "." in tok:
            continue
        # require end or whitespace/punctuation after token
        if end < len(blob) and re.match(r"[A-Za-z0-9_\-]", blob[end]):
            continue
        found.append(tok)
    return found


def extract_skill_name(user_text: str, tools: list[CanonicalTool] | None = None) -> str | None:
    blob = user_text or ""
    slashes = extract_slash_commands(blob)
    if slashes:
        return slashes[0]
    m = re.search(
        r"(?:skill|技能|use_skill|Skill)\s*[:=]?\s*[`'\"]?([A-Za-z][A-Za-z0-9_\-.]{1,63})",
        blob,
        re.I,
    )
    if m:
        return m.group(1)
    return None


def skill_arg_key(router: CanonicalTool) -> str:
    params = router.parameters if isinstance(router.parameters, dict) else {}
    props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
    required = params.get("required") if isinstance(params.get("required"), list) else []
    for key in ("name", "skill", "skill_name", "id", "skill_id"):
        if key in props or key in required:
            return key
    return "name"


def adapt_args_for_tool(tool: CanonicalTool, args: dict[str, Any]) -> dict[str, Any]:
    if not args:
        return {}
    params = tool.parameters if isinstance(tool.parameters, dict) else {}
    props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
    out = dict(args)
    if not props:
        return out

    groups = {
        "path": ("path", "file_path", "file", "filename", "filepath", "target"),
        "content": ("content", "text", "data", "body", "contents"),
        "command": ("command", "cmd", "input", "script", "code"),
        "query": ("query", "q", "search", "prompt", "keyword", "text", "input"),
        "name": ("name", "skill", "skill_name", "id", "skill_id"),
        "old": ("old", "old_string", "old_str", "search"),
        "new": ("new", "new_string", "new_str", "replace"),
        "patch": ("patch", "diff", "input", "update"),
    }

    present_syn: dict[str, Any] = {}
    for canon, syns in groups.items():
        for s in syns:
            if s in out and out[s] is not None:
                present_syn[canon] = out[s]
                break

    for prop in props:
        if prop in out:
            continue
        for canon, syns in groups.items():
            if prop in syns and canon in present_syn:
                out[prop] = present_syn[canon]
                break

    filtered = {k: v for k, v in out.items() if k in props}
    return filtered or out


def make_skill_router_call(
    router: CanonicalTool,
    skill_name: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = skill_arg_key(router)
    args: dict[str, Any] = {key: skill_name}
    if extra:
        args.update(extra)
    args = adapt_args_for_tool(router, args)
    return {
        "id": f"call_{uuid.uuid4().hex[:20]}",
        "type": "function",
        "function": {
            "name": router.name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def resolve_forced_call(
    tools: list[CanonicalTool],
    user_text: str,
) -> dict[str, Any] | None:
    """Multi-platform force decision.

    1. concrete registered tool named in text -> None (scored pick)
    2. /skill or skill mention + skill router -> use_skill{name}
    3. else None
    """
    if not tools:
        return None
    blob = user_text or ""

    for t in tools:
        if is_skill_router(t):
            continue
        n = t.name or ""
        if not n:
            continue
        if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", blob, re.I):
            return None
        fam = family_of(n)
        if fam == "write" and re.search(r"(?i)\bwrite\b|写入|写到|写文件|保存到|创建文件", blob):
            return None
        if fam == "shell" and re.search(r"(?i)\bbash\b|\bshell\b|终端|执行命令", blob):
            return None
        if fam == "web_search" and re.search(r"(?i)tavily|web_search|搜索工具", blob):
            return None
        if fam == "read" and re.search(r"(?i)\bread\b|读取文件|读一下", blob):
            return None

    router = find_skill_router(tools)
    skill = extract_skill_name(user_text, tools)
    if router and skill:
        direct = next(
            (x for x in tools if x.name == skill or x.name.lower() == skill.lower()),
            None,
        )
        if direct and not is_skill_router(direct):
            return None
        return make_skill_router_call(router, skill)
    return None


def platform_preamble_extra(tools: list[CanonicalTool]) -> str:
    lines: list[str] = []
    router = find_skill_router(tools)
    if router:
        key = skill_arg_key(router)
        lines.append(
            f"SKILL ROUTER: tool `{router.name}` loads skills. "
            f"For /foo or skill foo emit:\n"
            f"```{router.name}\n"
            f'{{"{key}":"foo"}}\n'
            f"```\n"
            f"Do NOT invent a bare tool named foo unless it is listed above."
        )
    fams = {family_of(t.name) for t in tools}
    if "write" in fams:
        lines.append("Write tools need path + content JSON.")
    if "shell" in fams:
        lines.append('Shell tools need {"command":"..."}.')
    if "web_search" in fams:
        lines.append('Search tools need {"query":"..."}.')
    return "\n".join(lines)



def should_short_circuit(tools: list[CanonicalTool], user_text: str) -> dict | None:
    """Return forced tool_call for unambiguous tool intents; skip the model."""
    if not tools or not (user_text or "").strip():
        return None
    blob = (user_text or "").strip()

    # 1) skill router / slash
    routed = resolve_forced_call(tools, blob)
    if routed:
        return routed

    slashes = extract_slash_commands(blob)
    router = find_skill_router(tools)
    if slashes:
        tok = slashes[0]
        if router:
            return make_skill_router_call(router, tok)
        for tool in tools:
            if tool.name == tok or tool.name.lower() == tok.lower():
                return {
                    "id": f"call_{uuid.uuid4().hex[:20]}",
                    "type": "function",
                    "function": {"name": tool.name, "arguments": "{}"},
                }
        # last resort: only one tool registered → call it with name arg if possible
        if len(tools) == 1:
            only = tools[0]
            args = adapt_args_for_tool(only, {"name": tok, "skill": tok, "command": tok, "query": tok})
            if not args:
                # skill-like arg key
                args = {skill_arg_key(only): tok} if is_skill_router(only) else {"name": tok}
            return {
                "id": f"call_{uuid.uuid4().hex[:20]}",
                "type": "function",
                "function": {
                    "name": only.name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }

    # 2) message is exactly a tool name
    for tool in tools:
        if re.fullmatch(rf"\s*{re.escape(tool.name)}\s*", blob, re.I):
            return {
                "id": f"call_{uuid.uuid4().hex[:20]}",
                "type": "function",
                "function": {"name": tool.name, "arguments": "{}"},
            }
    return None
