"""Tool-call repair + force-fallback when the model narrates instead of calling."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from mcg.compat.canonical import CanonicalRequest, CanonicalTool
from mcg.tools.inject import request_has_tool_results
from mcg.tools.loop import ToolLoop, ParsedTools
from mcg.tools.platform_adapt import (
    resolve_forced_call,
    adapt_args_for_tool,
    is_skill_router,
    family_of,
)

# Hard refuse → skip extra repair round (saves 5–15s latency)
_HARD_REFUSE = re.compile(
    r"(?is)cannot call|can't call|unable to call|is not available|"
    r"isn't available|not available|i can only invoke|"
    r"clarifying tool|tool limitations|do not have access|"
    r"don't have access|no access to|"
    r"没有提供.*文件|无法.*写入|不能.*部署|不能实际执行|"
    r"no (?:file|write|filesystem)|cannot (?:write|deploy|create)|"
    r"lack.*(?:file|write|filesystem)|filesystem access|"
    r"不是.*(?:工具|技能)|不可调用|无法调用|没有.*技能|"
    r"not (?:a |an )?(?:available )?tool|not.*skill name|"
    r"当前环境中可调用"
)



def _required_keys(tool: CanonicalTool) -> list[str]:
    params = tool.parameters or {}
    if not isinstance(params, dict):
        return []
    req = params.get("required") or []
    return list(req) if isinstance(req, list) else []


def _user_blob(canon: CanonicalRequest) -> str:
    parts: list[str] = []
    for m in canon.messages:
        if m.role == "user" and m.content:
            parts.append(m.content)
    return "\n".join(parts)


def _extract_query(user_text: str, tool_name: str = "") -> str:
    """Pull the actual search/query payload out of natural language."""
    blob = (user_text or "").strip()
    if not blob:
        return ""
    name_l = (tool_name or "").lower()
    # strip slash command
    blob2 = re.sub(r"^/[A-Za-z0-9_\-\.]+\s*", "", blob).strip()
    # "调用我的tavily搜索工具查今天新闻" / "use tavily to search X"
    patterns = [
        rf"(?:用|调用|使用|call|use)?\s*(?:我的)?\s*{re.escape(name_l)}\s*(?:搜索)?(?:工具)?\s*(?:来)?\s*(?:查|搜|search|查找|检索)?\s*(.+)$",
        r"(?:用|调用|使用)\s*(?:我的)?\s*[\w\-\.\u4e00-\u9fff]{2,40}\s*(?:搜索)?(?:工具)?\s*(?:来)?\s*(?:查|搜|search|查找|检索)\s*(.+)$",
        r"(?:搜索|search|查|查找|检索|google)\s*(?:一下)?\s*(.+)$",
        r"(?:run|执行|运行)\s+(.+)$",
    ]
    for pat in patterns:
        if "{re.escape" in pat:
            continue
        m = re.search(pat, blob2, re.I)
        if m:
            q = m.group(1).strip(" 。.!！?？:：")
            # drop leftover "工具" prefix
            q = re.sub(r"^(?:工具|tool)\s*", "", q)
            if q:
                return q
    # name-specific after building escape
    if name_l:
        m = re.search(
            rf"(?:用|调用|使用|call|use)?\s*(?:我的)?\s*{re.escape(name_l)}\s*(?:搜索)?(?:工具)?\s*(?:来)?\s*(?:查|搜|search|查找|检索)?\s*(.+)$",
            blob2,
            re.I,
        )
        if m:
            q = m.group(1).strip(" 。.!！?？:：")
            q = re.sub(r"^(?:工具|tool)\s*", "", q)
            if q:
                return q
    return blob2 or blob


def _extract_command(user_text: str) -> str:
    blob = (user_text or "").strip()
    m = re.search(r"(?:run|执行|运行|bash|shell)\s*(?:工具)?\s*(?:执行|运行)?\s*[:=]?\s*(.+)$", blob, re.I)
    if m:
        cmd = m.group(1).strip()
        cmd = re.sub(r"^(?:执行|运行|命令)\s*", "", cmd)
        return cmd
    # ```bash ... ```
    m = re.search(r"```(?:bash|sh|shell)?\s*\n?(.*?)```", blob, re.I | re.S)
    if m:
        return m.group(1).strip()
    return blob


def _extract_write(user_text: str):
    """Return (path, content) best-effort from NL."""
    blob = (user_text or "").strip()
    path = None
    m = re.search(r"(/[\w.\-]+(?:/[\w.\-]+)+)", blob)
    if not m:
        m = re.search(r"([A-Za-z]:\\[\w.\\\-]+)", blob)
    if m:
        path = m.group(1)
    content = None
    m = re.search(r'(?:把|将)\s*["\']?(.+?)["\']?\s*(?:写入|写到|保存到|写进)', blob)
    if m:
        content = m.group(1).strip()
    if not content:
        m = re.search(r'(?:write|content)\s*[:=]\s*["\'](.+?)["\']', blob, re.I)
        if m:
            content = m.group(1)
    if not content:
        m = re.search(r'["\']([^"\']{1,200})["\']', blob)
        if m:
            content = m.group(1)
    return path, content


def _guess_args(tool: CanonicalTool, user_text: str) -> dict[str, Any]:
    """Best-effort fill required string args from last user text."""
    params = tool.parameters if isinstance(tool.parameters, dict) else {}
    props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
    required = _required_keys(tool)
    args: dict[str, Any] = {}
    blob = (user_text or "").strip()
    name_l = tool.name.lower()
    is_shell = any(k in name_l for k in ("bash", "shell", "run", "exec", "cmd", "terminal"))
    is_search = any(k in name_l for k in ("search", "tavily", "web", "搜", "query", "news"))
    is_write = any(k in name_l for k in ("write", "save", "create_file")) or family_of(tool.name)=="write"
    query = _extract_query(blob, tool.name) if is_search else blob
    command = _extract_command(blob) if is_shell else blob
    w_path, w_content = _extract_write(blob) if is_write else (None, None)

    keys = list(required)
    if not keys and isinstance(props, dict):
        for k in ("query", "q", "command", "cmd", "input", "prompt", "path", "content"):
            if k in props and k not in keys:
                keys.append(k)
    if is_write and isinstance(props, dict):
        for k in ("path", "content", "file_path", "text"):
            if k in props and k not in keys:
                keys.append(k)

    for key in keys:
        prop = props.get(key) if isinstance(props, dict) else None
        ptype = (prop or {}).get("type") if isinstance(prop, dict) else None
        if ptype in (None, "string"):
            if key in ("command", "cmd", "script", "code"):
                args[key] = command
            elif key in ("query", "q", "search", "keyword", "prompt"):
                args[key] = query if is_search else blob
            elif key in ("content", "body", "data") and is_write:
                args[key] = w_content if w_content is not None else blob
            elif key in ("input", "text") and is_write:
                args[key] = w_content if w_content is not None else blob
            elif key in ("input", "text"):
                args[key] = query if is_search else blob
            elif key in ("path", "file_path", "file", "filename") and is_write and w_path:
                args[key] = w_path
            elif key in ("city", "location", "place"):
                m = re.search(r"(?:in|at|for|of)\s+([A-Za-z][A-Za-z\s\-]{1,40})", blob, re.I)
                args[key] = (m.group(1).strip(" ?.!,") if m else (blob.split()[-1] if blob else ""))
            elif key in ("path", "cwd", "dir", "directory"):
                # ignore pure slash-commands like /story-setup
                cleaned = re.sub(r"^/[A-Za-z0-9_\-\.]+\s*", "", blob).strip()
                m = re.search(r"((?:\.?\.?/|~/)[\w\-\./]+|[A-Za-z]:\\[\w\-\\]+)", cleaned)
                if m:
                    args[key] = m.group(1)
                elif cleaned:
                    args[key] = cleaned
                # else leave unset / empty
            else:
                args[key] = query if is_search else blob
        elif ptype in ("number", "integer"):
            m = re.search(r"-?\d+(?:\.\d+)?", blob)
            if m:
                args[key] = float(m.group(0)) if ptype == "number" else int(float(m.group(0)))
    if not args and is_shell:
        args = {"command": command}
    if not args and is_search:
        args = {"query": query}
    return args


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())


def _score_tool(tool: CanonicalTool, user_text: str) -> float:
    """Higher = better match for this user turn.

    Explicit name / slash-command wins hard. Description keyword hits next.
    Zero-required alone must NOT beat a named match (that caused time-tool hijack).
    """
    blob = user_text or ""
    blob_l = blob.lower()
    blob_n = _norm(blob)
    name = tool.name or ""
    name_l = name.lower()
    name_n = _norm(name)
    desc = (tool.description or "").lower()
    score = 0.0

    # slash command: /story-setup or /story_setup
    slash = re.findall(r"/([A-Za-z0-9_\-\.]{2,64})", blob)
    for tok in slash:
        tn = _norm(tok)
        if tn and (tn == name_n or tn in name_n or name_n in tn):
            score += 100.0
        # hyphen/underscore variants
        if tok.lower().replace("-", "_") == name_l.replace("-", "_"):
            score += 100.0

    # bare tool name mentioned
    if name_l and name_l in blob_l:
        score += 80.0
    if name_n and len(name_n) >= 3 and name_n in blob_n:
        score += 70.0

    # name tokens (tavily, search, web_search, story, setup)
    tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{3,}", name_l)
    for tok in tokens:
        if tok in blob_l or _norm(tok) in blob_n:
            score += 25.0
        # common aliases
        aliases = {
            "tavily": ("tavily", "tavili", "tavil", "搜索", "search", "web"),
            "search": ("search", "搜索", "news", "新闻", "查"),
            "web": ("web", "搜索", "search"),
            "bash": ("bash", "shell", "命令", "终端", "run"),
            "shell": ("shell", "bash", "命令"),
            "story": ("story", "故事", "小说", "网文"),
            "setup": ("setup", "设定", "开书"),
            "time": ("time", "时间", "date", "日期", "clock"),
            "datetime": ("time", "时间", "date", "日期"),
        }
        for key, words in aliases.items():
            if key in tok or tok in key:
                if any(w in blob_l or _norm(w) in blob_n for w in words):
                    score += 15.0

    # description keyword overlap
    for w in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", desc):
        if len(w) >= 2 and (w in blob_l or _norm(w) in blob_n):
            score += 3.0

    # user intent: search/news → boost search-like tools, demote pure time tools
    wants_search = bool(
        re.search(r"搜索|search|news|新闻|查一下|查找|检索|tavily|google|web", blob_l)
    )
    wants_time = bool(re.search(r"几点|什么时候|当前时间|现在时间|what time|date now", blob_l))
    is_time = bool(re.search(r"time|date|clock|时间|日期", name_l + " " + desc))
    is_search = bool(re.search(r"search|tavily|web|搜|新闻|query", name_l + " " + desc))
    if wants_search and is_search:
        score += 40.0
    if wants_search and is_time and not wants_time:
        score -= 50.0
    if wants_time and is_time:
        score += 40.0

    wants_write = bool(
        re.search(r"写(?:一个|个|入|文件|到)?|write|create\s*file|保存|新建.*文件", blob_l)
    )
    is_write = bool(re.search(r"write|save|create_file|写", name_l + " " + desc)) or (
        family_of(tool.name) == "write"
    )
    if wants_write and is_write:
        score += 45.0
    if wants_write and is_search and not wants_search:
        score -= 30.0

    # tiny bias for zero-required ONLY as tie-breaker, never primary
    if not _required_keys(tool):
        score += 0.5
    return score


_CHATTY_RE = re.compile(
    r"(?is)^\s*(?:"
    r"你好|您好|嗨|哈喽|在吗|在不在|早上好|晚上好|下午好|"
    r"hi|hello|hey|yo|sup|good\s*(?:morning|evening|afternoon)|"
    r"thanks?|thank\s*you|谢谢|多谢|ok|okay|好的|嗯|哦|啊|"
    r"测试|test|ping|pong|你是谁|你叫什么"
    r")[.!！?？。…\s]*$"
)


def is_plain_chat(user_text: str) -> bool:
    """True when user is just chatting — do NOT force any tool."""
    blob = (user_text or "").strip()
    if not blob:
        return True
    if blob.startswith("/"):
        return False
    if _CHATTY_RE.match(blob):
        return True
    # very short with no tool-ish keywords
    if len(blob) <= 6 and not re.search(
        r"工具|tool|搜索|search|写|write|bash|shell|skill|技能|新闻|news|部署|setup",
        blob,
        re.I,
    ):
        return True
    return False


def pick_tool(tools: list[CanonicalTool], user_text: str = "") -> CanonicalTool | None:
    if not tools:
        return None
    ranked = sorted(tools, key=lambda t: _score_tool(t, user_text), reverse=True)
    best = ranked[0]
    best_score = _score_tool(best, user_text)
    # No signal at all → refuse to invent a tool (was: always pick search/first)
    if best_score < 5.0:
        return None
    return best


def force_tool_call(
    tools: list[CanonicalTool],
    *,
    user_text: str = "",
) -> list[dict[str, Any]]:
    """Emit a tool call only when intent is clear. Greetings → []."""
    if not tools:
        return []
    if is_plain_chat(user_text):
        return []
    # 1) /story-setup or skill mention with use_skill present
    routed = resolve_forced_call(tools, user_text)
    if routed:
        return [routed]
    # 2) scored pick — None when score too low (no hijack to search_web)
    pick = pick_tool(tools, user_text)
    if pick is None:
        return []
    args = _guess_args(pick, user_text)
    args = adapt_args_for_tool(pick, args)
    # skill router without extractable skill name: still try slash
    if is_skill_router(pick):
        from mcg.tools.platform_adapt import extract_skill_name, make_skill_router_call
        skill = extract_skill_name(user_text, tools)
        if skill:
            return [make_skill_router_call(pick, skill)]
        # bare skill router without a skill name is not useful
        return []
    return [
        {
            "id": f"call_{uuid.uuid4().hex[:20]}",
            "type": "function",
            "function": {
                "name": pick.name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
    ]


def build_repair_prompt(tools: list[CanonicalTool], previous: str) -> str:
    primary = tools[0].name if tools else "tool"
    names = ", ".join(t.name for t in tools)
    req = _required_keys(tools[0]) if tools else []
    sample = {k: f"<{k}>" for k in req} if req else {}
    body = json.dumps(sample, ensure_ascii=False)
    return (
        f"INVALID. Emit tool call only. Tools: {names}\n"
        f"```{primary}\n"
        f"{body}\n"
        "```"
    )



def _last_tool_result_blob(canon: CanonicalRequest) -> str:
    parts = []
    for m in canon.messages:
        if m.role == "tool" and m.content:
            parts.append(m.content)
    return "\n".join(parts)


def needs_tool_chain(canon: CanonicalRequest) -> bool:
    """True when prior tool_results imply more tools should run (skill deploy etc.)."""
    if not request_has_tool_results(canon) or not canon.tools:
        return False
    blob = _last_tool_result_blob(canon).lower()
    if not blob:
        return False
    # Terminal success — stop chaining (prevents Bash/Write loops)
    done_markers = (
        "deployed",
        ".story-deployed",
        "already deployed",
        "setup complete",
        "部署完成",
        "已部署",
        "created successfully",
        "writing infra",
    )
    # last tool message only
    last = ""
    for m in reversed(canon.messages):
        if m.role == "tool" and m.content:
            last = m.content.lower()
            break
    if any(k in last for k in done_markers) and "not_deployed" not in last and "error" not in last[:200]:
        # if last action was write/shell (not skill), treat as done
        last_call_skill = False
        for m in reversed(canon.messages):
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    fn = ((tc.get("function") or {}).get("name") or "").lower()
                    if "skill" in fn:
                        last_call_skill = True
                break
        if not last_call_skill:
            return False
    # skill content / deploy instructions
    skillish = any(
        k in blob
        for k in (
            "story-setup",
            "skill",
            "phase 1",
            "phase 2",
            "部署",
            "clau demd".replace(" ", ""),
            "claude.md",
            ".claude/",
            "hooks",
            "write",
            "create",
            "mkdir",
            "部署基础设施",
            "执行铁律",
            "settings.local.json",
            ".story-deployed",
        )
    )
    # also: model previously only loaded skill
    prior_calls = []
    for m in canon.messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                fn = (tc.get("function") or {}).get("name") or ""
                prior_calls.append(fn)
    only_skill = prior_calls and all(
        "skill" in (n or "").lower() for n in prior_calls
    )
    has_write = any(family_of(t.name) in ("write", "shell", "edit", "read") for t in canon.tools)
    if (skillish or only_skill) and has_write:
        return True
    # generic incomplete: user asked deploy/setup and only skill returned
    user_blob = _user_blob(canon).lower()
    if has_write and any(k in user_blob for k in ("setup", "部署", "初始化", "scaffold", "install")):
        if only_skill or skillish:
            return True
    return False


def _pick_family(candidates: list[CanonicalTool], fam: str) -> CanonicalTool | None:
    """Prefer real local tools over false-positive family matches."""
    hits = [t for t in candidates if family_of(t.name) == fam]
    if not hits:
        return None
    if fam == "shell":
        def shell_rank(t: CanonicalTool) -> float:
            n = (t.name or "").lower()
            score = 0.0
            if any(k in n for k in ("bash", "shell", "terminal", "execute_shell", "run_terminal", "local_shell")):
                score += 100
            if n in ("bash", "shell", "sh"):
                score += 50
            # has command param
            props = (t.parameters or {}).get("properties") if isinstance(t.parameters, dict) else {}
            if isinstance(props, dict) and any(k in props for k in ("command", "cmd", "script")):
                score += 40
            # github / cloud names hard demote
            if any(k in n for k in ("push_files", "github", "pull_request", "repository", "vercel", "email")):
                score -= 200
            return score
        hits.sort(key=shell_rank, reverse=True)
        if shell_rank(hits[0]) <= 0:
            return None
        return hits[0]
    if fam == "write":
        def write_rank(t: CanonicalTool) -> float:
            n = (t.name or "").lower()
            score = 0.0
            if any(k in n for k in ("write", "create_file", "file_write", "write_to_file")):
                score += 100
            if any(k in n for k in ("push_files", "github", "pull_request", "repository")):
                score -= 200
            props = (t.parameters or {}).get("properties") if isinstance(t.parameters, dict) else {}
            if isinstance(props, dict) and any(k in props for k in ("path", "file_path", "content")):
                score += 30
            return score
        hits.sort(key=write_rank, reverse=True)
        if write_rank(hits[0]) <= 0:
            return None
        return hits[0]
    return hits[0]


def force_chain_tool_call(canon: CanonicalRequest) -> list[dict[str, Any]]:
    """After skill load, force the first concrete action tool (Write/Bash preferred)."""
    user_text = _user_blob(canon)
    result_blob = _last_tool_result_blob(canon)
    candidates = [t for t in canon.tools if not is_skill_router(t)]
    if not candidates:
        candidates = list(canon.tools)

    shell = _pick_family(candidates, "shell")
    write = _pick_family(candidates, "write")
    skillish = any(
        k in (result_blob or "").lower()
        for k in ("story-setup", "claude.md", ".claude/", "部署", "phase")
    )

    if shell and skillish:
        pick = shell
        args = {
            "command": (
                "mkdir -p .claude/hooks .claude/rules .claude/agents && "
                "touch .story-deployed && "
                "printf '%s\\n' '# Project writing infra' > CLAUDE.md && "
                "ls -la .claude .story-deployed CLAUDE.md 2>/dev/null || ls -la"
            )
        }
    elif write:
        pick = write
        args = _guess_args(pick, user_text)
        args = adapt_args_for_tool(pick, args)
        path = str(args.get("path") or args.get("file_path") or "")
        if (not path) or path.startswith("/hooks") or "Phase" in path or path.count("/") > 3 and "tmp" not in path:
            # skill body often pollutes path extraction — use marker file
            args["path"] = ".story-deployed"
        content = args.get("content") or args.get("text")
        if (not content) or len(str(content)) > 200 or "story-setup" in str(content) or "Phase" in str(content):
            args["content"] = "deployed\n"
            args.pop("text", None)
    else:
        scored = []
        for t in candidates:
            fam = family_of(t.name) or ""
            score = {"write": 50, "shell": 45, "edit": 30, "read": 10}.get(fam, 0)
            scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] <= 0:
            # no deploy-capable tool — caller should passthrough skill text
            return []
        pick = scored[0][1]
        args = _guess_args(pick, user_text)
        args = adapt_args_for_tool(pick, args)

    return [
        {
            "id": f"call_{uuid.uuid4().hex[:20]}",
            "type": "function",
            "function": {
                "name": pick.name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
    ]




def looks_like_failed_tool_turn(text: str, tools: list[CanonicalTool], had_tools: bool) -> bool:
    """Backward-compatible: narrate/refuse instead of tool call."""
    if not had_tools or not tools:
        return False
    if not (text or "").strip():
        return True
    if _HARD_REFUSE.search(text):
        return True
    # talk about tools without emitting a call
    if re.search(r"(?i)reading relevant|i need to review|let me (?:check|read|call)", text):
        return True
    return False

async def maybe_repair_tool_call(
    *,
    client: Any,
    tool_loop: ToolLoop,
    canon: CanonicalRequest,
    stream_kwargs: dict[str, Any],
    full_text: str,
    repair_rounds: int,
    force_if_empty: bool = True,
) -> ParsedTools:
    parsed = tool_loop.parse(full_text, canon.tools)
    if parsed.tool_calls or not canon.tools:
        return parsed

    # After tool results: allow multi-hop chain (skill → Write/Bash). Never re-call skill.
    if request_has_tool_results(canon):
        if force_if_empty and needs_tool_chain(canon):
            refuse = bool(full_text and (
                _HARD_REFUSE.search(full_text)
                or re.search(
                    r"没有.*文件|无法.*写入|不能.*部署|no (?:file|write|filesystem)|cannot (?:write|deploy|create)|lack.*access|没有提供.*文件",
                    full_text,
                    re.I,
                )
            ))
            no_tool = not parsed.tool_calls
            # if model already emitted a non-skill tool call, keep it
            if parsed.tool_calls:
                names = []
                for tc in parsed.tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    names.append((fn.get("name") or "").lower())
                if names and all("skill" in n for n in names):
                    # model tried to re-call skill → replace with chain
                    return ParsedTools(text="", tool_calls=force_chain_tool_call(canon))
                return parsed
            if refuse or no_tool:
                return ParsedTools(text="", tool_calls=force_chain_tool_call(canon))
        return parsed

    user_text = _user_blob(canon)

    if force_if_empty and full_text and _HARD_REFUSE.search(full_text):
        return ParsedTools(
            text="", tool_calls=force_tool_call(canon.tools, user_text=user_text)
        )

    if repair_rounds > 0 and full_text:
        kwargs = dict(stream_kwargs)
        kwargs["is_start_of_session"] = False
        kwargs["message_extras"] = None
        kwargs["agent_id"] = None
        tone = str(kwargs.get("tone") or "")
        if "Reasoning" in tone or tone in ("Gpt_Quick",):
            kwargs["tone"] = "Magic"
        text = full_text
        rounds = 0
        while rounds < repair_rounds and not parsed.tool_calls:
            rounds += 1
            text = await client.chat(build_repair_prompt(canon.tools, text), **kwargs)
            parsed = tool_loop.parse(text, canon.tools)

    if not parsed.tool_calls and force_if_empty:
        return ParsedTools(
            text="", tool_calls=force_tool_call(canon.tools, user_text=user_text)
        )
    return parsed
