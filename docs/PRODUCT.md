# Product plan — authoritative M365 Copilot gateway

## Positioning

Most complete self-hosted reverse proxy for **M365 enterprise Copilot (Substrate)**:

- Real ChatHub WebSocket (not Graph-only, not GitHub Copilot, not consumer Bing Copilot)
- Multi-account pool + semi-auto import
- WebUI management
- Gateway auth
- Auto-advertised models / tones
- Multi-agent tool compatibility without server-side tool registration

## Stack decision

| Choice | Decision | Why |
|--------|----------|-----|
| Language | **Python 3.11+** | Lightest iteration cost; kuchris/HEXUXIU already validate Substrate in Python; WebUI + admin APIs fast |
| HTTP | FastAPI + uvicorn | ASGI, OpenAPI free, SSE easy |
| WS | `websockets` | Same as kuchris/HEXUXIU |
| UI | Server-rendered HTML + minimal JS first | No Node build for P0; can upgrade later |
| Browser | Playwright optional extra | CDP token capture without always-on browser |

Not TS/Bun/Rust for v1: heavier monorepo / compile loop for the same protocol.

## Capability matrix

### Must-have (P0–P2)

1. Substrate streaming chat
2. `POST /v1/chat/completions` stream + non-stream
3. `GET /v1/models` from config + live probe
4. Gateway API key auth
5. Account pool CRUD
6. Token hot cache + expiry
7. Semi-auto account import (paste JWT + browser capture stub)
8. WebUI: login, accounts, models, health
9. Dynamic tools from request (OpenAI tools format)
10. Attribution docs + protocol notes

### Should-have (P3–P4)

1. Anthropic `/v1/messages`
2. OpenAI Responses API
3. Shell-route + fenced tool strategies (cramt-inspired)
4. Image input/output adapters
5. Audio via STT plugin
6. Frame dump / calibrate CLI
7. Docker Compose

### Won't pretend

- Microsoft-native OpenAI function calling (does not exist on ChatHub)
- Unlimited free access without a licensed session
- Stable SLA against undocumented API breakage

## Tooling model (multi-agent)

```
Request tools[]  →  ToolSpec[] (ephemeral)
                 →  prompt inject (fenced/json/shell)
                 →  Substrate turn
                 →  parse tool_calls
                 →  return to client (execution=client)
                 →  client posts tool results
                 →  next Substrate turn
```

No global tool registry. Claude Code / Cursor / OpenAI SDK all map through `compat/`.

## Account pool

```
Account {
  id, label, substrate_token, profile_path?,
  status: active|cooldown|disabled|expired,
  errors, last_used, last_success, meta
}
```

Import paths:

1. WebUI paste token
2. `mcg account import-token --file`
3. `mcg account browser-login` (Playwright/CDP) → semi-auto

## WebUI pages

- `/ui` dashboard
- `/ui/accounts` pool + import
- `/ui/models` advertised models
- `/ui/logs` recent requests (ring buffer)
- `/ui/settings` bind, keys (hashed display)

## Milestones

| ID | Deliverable | Exit criteria |
|----|-------------|----------------|
| M1 | Repo + config + serve skeleton | `mcg serve` listens, health 200 |
| M2 | Substrate client + fold stream | live chat with one token |
| M3 | OpenAI completions + auth | curl chat works |
| M4 | Pool + import paste | multi-account switch |
| M5 | WebUI shell | manage accounts in browser |
| M6 | Dynamic tools loop | Cursor/OpenAI tools round-trip |
| M7 | Anthropic + models auto | Claude Code usable |
| M8 | Docker + docs polish | one-command deploy |

## GitHub

- Public or private under operator account
- README + ATTRIBUTIONS required on every release
- Changelog tracks protocol breakage
