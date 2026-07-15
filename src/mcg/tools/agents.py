"""Agent client fingerprint + profiles.

Detect Claude Code / Codex / Cursor / Cline / Continue / OpenClaw / phone-lite
from headers + tool names, then apply wire-format and tool-policy quirks so the
gateway does not fall into known traps (reasoning prose refuse, skill loops,
missing Write/Bash, Anthropic tool_result lists, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from mcg.compat.canonical import CanonicalTool
from mcg.tools.platform_adapt import family_of, is_skill_router


# --- known tool-name fingerprints ------------------------------------------------

_CC_MARKERS = frozenset(
    {
        "bash",
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "webfetch",
        "websearch",
        "todowrite",
        "task",
        "agent",
        "notebookedit",
        "skill",
        "useskill",
    }
)
_CODEX_MARKERS = frozenset(
    {
        "shell",
        "local_shell",
        "apply_patch",
        "applypatch",
        "update_plan",
        "updateplan",
        "web_search",
        "websearch",
        "view_image",
        "list_dir",
    }
)
_CURSOR_MARKERS = frozenset(
    {
        "codebase_search",
        "read_file",
        "run_terminal_cmd",
        "edit_file",
        "search_replace",
        "list_dir",
        "grep_search",
        "file_search",
        "delete_file",
        "reapply",
        "web_search",
    }
)
_CLINE_MARKERS = frozenset(
    {
        "write_to_file",
        "read_file",
        "execute_command",
        "replace_in_file",
        "search_files",
        "list_files",
        "browser_action",
        "ask_followup_question",
        "attempt_completion",
    }
)
_CONTINUE_MARKERS = frozenset(
    {
        "builtin_read_file",
        "builtin_edit_file",
        "builtin_run_terminal_command",
        "builtin_grep_search",
        "run_terminal_command",
    }
)
_OPENCLAW_MARKERS = frozenset(
    {
        "use_skill",
        "run_skill",
        "invoke_skill",
        "load_skill",
    }
)
_PHONE_LITE = frozenset(
    {
        "search_web",
        "scrape_web",
        "get_time_info",
        "use_skill",
        "tavily_search",
        "web_search_tavily",
        "获取时间信息",
    }
)


@dataclass(frozen=True)
class AgentProfile:
    id: str
    label: str
    # Prefer Anthropic Messages wire for skill/tool_use
    wire: str = "openai"  # openai | anthropic | responses
    # Hop1 slash → skill short-circuit
    slash_skill: bool = True
    # Hop2 skill result: chain Write/Bash if present
    hop2_chain: bool = True
    # Hop2 without file tools: passthrough skill markdown (never prose-refuse)
    hop2_passthrough: bool = True
    # Force Chat/Magic tone when tools present (already global; profile may force harder)
    force_non_reasoning: bool = True
    # Emit Anthropic tool_use ids with toolu_ prefix when wire=anthropic
    tool_id_prefix: str = "call_"
    notes: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


PROFILES: dict[str, AgentProfile] = {
    "claude_code": AgentProfile(
        id="claude_code",
        label="Claude Code",
        wire="anthropic",
        tool_id_prefix="toolu_",
        notes=(
            "Uses /v1/messages + input_schema tools",
            "tool_result content may be list[{type:text}]",
            "Built-ins: Bash/Read/Write/Edit/Glob/Grep/Skill",
            "Never re-force Skill after tool_result hop",
        ),
    ),
    "codex": AgentProfile(
        id="codex",
        label="OpenAI Codex CLI",
        wire="openai",
        notes=(
            "Shell + apply_patch primary; avoid inventing Write",
            "May use Responses API later; chat/completions still common",
            "Prefer shell over synthetic Write when both absent names differ",
        ),
    ),
    "cursor": AgentProfile(
        id="cursor",
        label="Cursor",
        wire="openai",
        notes=(
            "run_terminal_cmd / read_file / edit_file / codebase_search",
            "Heavy system prompts; keep tool preamble compact",
        ),
    ),
    "cline": AgentProfile(
        id="cline",
        label="Cline / Roo",
        wire="openai",
        notes=(
            "write_to_file / execute_command / attempt_completion",
            "XML-ish tool habits in older builds — fence still accepted",
        ),
    ),
    "continue": AgentProfile(
        id="continue",
        label="Continue.dev",
        wire="openai",
        notes=("builtin_* tool names",),
    ),
    "openclaw": AgentProfile(
        id="openclaw",
        label="OpenClaw / skill-first",
        wire="openai",
        notes=("use_skill router + optional Write/Bash",),
    ),
    "phone_lite": AgentProfile(
        id="phone_lite",
        label="Phone / lite client",
        wire="openai",
        hop2_chain=False,
        hop2_passthrough=True,
        notes=(
            "Only use_skill + web + time — no Write/Bash",
            "Hop2 must passthrough skill body; never model refuse",
        ),
    ),
    "generic": AgentProfile(
        id="generic",
        label="Generic OpenAI/Anthropic client",
        wire="openai",
    ),
}


def _norm_names(tools: Iterable[CanonicalTool | str]) -> set[str]:
    out: set[str] = set()
    for t in tools or []:
        name = t if isinstance(t, str) else (t.name or "")
        n = name.strip().lower().replace("-", "").replace(" ", "")
        if n:
            out.add(n)
            # also bare lower with underscore kept for marker sets that use _
            out.add(name.strip().lower())
    return out


def detect_agent(
    *,
    tools: list[CanonicalTool] | None = None,
    headers: dict[str, str] | None = None,
    user_agent: str | None = None,
    path: str | None = None,
    model: str | None = None,
) -> AgentProfile:
    """Best-effort fingerprint. Order: headers → path → tool-name heuristics."""
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    ua = (user_agent or hdrs.get("user-agent") or "").lower()
    path_l = (path or "").lower()

    # Explicit headers some forks send
    x_client = (hdrs.get("x-client") or hdrs.get("x-agent") or hdrs.get("x-title") or "").lower()
    if "claude" in ua or "claude-code" in ua or "claude code" in x_client:
        return PROFILES["claude_code"]
    if "codex" in ua or "openai-codex" in x_client:
        return PROFILES["codex"]
    if "cursor" in ua:
        return PROFILES["cursor"]
    if "cline" in ua or "roo" in ua:
        return PROFILES["cline"]
    if "continue" in ua:
        return PROFILES["continue"]

    if "/v1/messages" in path_l or path_l.endswith("/messages"):
        # Anthropic path — default CC-ish unless tools scream otherwise
        names = _norm_names(tools or [])
        if names & _CODEX_MARKERS and not (names & _CC_MARKERS):
            return PROFILES["codex"]
        return PROFILES["claude_code"]

    names = _norm_names(tools or [])
    if not names:
        return PROFILES["generic"]

    def score(markers: frozenset[str]) -> int:
        # markers may be underscored; normalize both sides
        mnorm = {m.replace("_", "").replace("-", "") for m in markers}
        nnorm = {n.replace("_", "").replace("-", "") for n in names}
        return len(mnorm & nnorm)

    scores = {
        "claude_code": score(_CC_MARKERS),
        "codex": score(_CODEX_MARKERS),
        "cursor": score(_CURSOR_MARKERS),
        "cline": score(_CLINE_MARKERS),
        "continue": score(_CONTINUE_MARKERS),
        "openclaw": score(_OPENCLAW_MARKERS),
    }
    # phone lite: small set, almost only web/time/skill
    phone_hit = score(_PHONE_LITE)
    has_file = any(family_of(t.name) in ("write", "shell", "edit", "read") for t in (tools or []))
    if phone_hit >= 2 and not has_file and len(names) <= 6:
        return PROFILES["phone_lite"]

    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] >= 2:
        return PROFILES[best[0]]
    if any(is_skill_router(t) for t in (tools or [])):
        return PROFILES["openclaw"]
    return PROFILES["generic"]


def has_deploy_tools(tools: list[CanonicalTool]) -> bool:
    return any(family_of(t.name) in ("write", "shell", "edit") for t in tools)


def agent_preamble_extra(profile: AgentProfile, tools: list[CanonicalTool]) -> str:
    """Tiny agent-specific addendum for tool preamble (keep short — reasoning models)."""
    bits: list[str] = [f"CLIENT={profile.id}"]
    if profile.id == "claude_code":
        bits.append("Claude Code: use exact tool names Bash/Read/Write/Edit; Skill for /slash.")
    elif profile.id == "codex":
        bits.append("Codex: prefer shell + apply_patch; JSON args only.")
    elif profile.id == "cursor":
        bits.append("Cursor: run_terminal_cmd/read_file/edit_file as listed.")
    elif profile.id == "phone_lite":
        bits.append(
            "Lite client: only skill/web/time. After use_skill returns, "
            "do NOT claim skill missing — summarize skill body or stop."
        )
    elif profile.id == "cline":
        bits.append("Cline: write_to_file/execute_command/attempt_completion.")
    if not has_deploy_tools(tools) and any(is_skill_router(t) for t in tools):
        bits.append("No Write/Bash in tools[] — never invent filesystem access denials after skill load.")
    return " ".join(bits)


def rewrite_tool_call_ids(
    tool_calls: list[dict[str, Any]] | None,
    profile: AgentProfile,
) -> list[dict[str, Any]] | None:
    """Normalize tool call id prefix for CC (toolu_) vs OpenAI (call_)."""
    if not tool_calls:
        return tool_calls
    prefix = profile.tool_id_prefix or "call_"
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        c = dict(tc)
        tid = str(c.get("id") or "")
        if prefix == "toolu_" and not tid.startswith("toolu_"):
            c["id"] = "toolu_" + tid.replace("call_", "")[-20:]
        elif prefix == "call_" and tid.startswith("toolu_"):
            c["id"] = "call_" + tid.replace("toolu_", "")[-20:]
        out.append(c)
    return out
