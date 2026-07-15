# Agent client adaptation (CC / Codex / Cursor / …)

Gateway fingerprint + policy lives in `src/mcg/tools/agents.py`.
Tool name families live in `src/mcg/tools/platform_adapt.py`.

## Detected profiles

| id | How detected | Wire | Pitfalls we avoid |
|----|--------------|------|-------------------|
| `claude_code` | UA / `/v1/messages` + Bash/Read/Write/Edit/Skill | Anthropic Messages | tool_result list content; `toolu_` ids; never re-Skill after result; hop2 chain Write/Bash |
| `codex` | UA / shell+apply_patch markers | OpenAI chat (Responses later) | Prefer shell/apply_patch over inventing Write; compact preamble |
| `cursor` | UA / run_terminal_cmd+codebase_search | OpenAI | Long system prompts — keep tool preamble short |
| `cline` | write_to_file/execute_command | OpenAI | Alias map to write/shell/edit |
| `continue` | builtin_* tools | OpenAI | Alias map |
| `openclaw` | use_skill present | OpenAI | Skill router short-circuit; hop2 chain |
| `phone_lite` | only skill+web+time, no file tools | OpenAI | **Hop2 passthrough skill body** — never model "not a skill" prose |
| `generic` | fallback | OpenAI | Safe defaults |

## Common traps (all agents)

1. **Reasoning tones + tools** → prose refuse. We force non-reasoning tone when `tools[]` present.
2. **`/story-setup` every hop** → infinite `use_skill`. Short-circuit only when latest user turn has **no** assistant/tool after it.
3. **Skill result + no Write/Bash** → model invents "skill unavailable". Hop2 passthrough skill markdown.
4. **Slash vs path** → `/tmp/a.txt` is not a skill; only `/[A-Za-z]…` tokens.
5. **Anthropic tool_result** → `content` may be `string | list[{type:text}]`; we flatten + honor `is_error`.
6. **Legacy `functions`** → merged into `tools` on OpenAI request model.
7. **0 tools + slash** → synthetic `use_skill` (some UIs still execute it).
8. **force_chain after deploy success** → done markers stop chain (no Bash loop).

## Client config cheat-sheet

### Claude Code
```
ANTHROPIC_BASE_URL=http://HOST:PORT
ANTHROPIC_API_KEY=<gateway key>
# endpoint: POST /v1/messages
```
Tools arrive as Anthropic `input_schema`. Gateway short-circuits `/skill` → `Skill`/`use_skill`.

### Codex CLI
```
# OpenAI-compatible base
OPENAI_BASE_URL=http://HOST:PORT/v1
OPENAI_API_KEY=<gateway key>
```
Expect `shell` / `apply_patch`. Do not require Write.

### Cursor / Cline / Continue
Point OpenAI base URL at `/v1`. Tools names remapped via family aliases.

### Phone / lite (AstrBot etc.)
If only `use_skill`+search+time: hop1 skill call, hop2 returns skill text. To actually deploy, register Write+Bash (or shell).

## Logs to watch
```
[mcg.agent] id=phone_lite tools=[...]
[mcg.tools] SHORT-CIRCUIT → ['use_skill']
[mcg.tools] HOP2 PASSTHROUGH skill='story-setup' ...
[mcg.tools] HOP2 CHAIN → ['Bash']
```
