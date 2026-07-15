# M365 Copilot Gateway

**Authoritative Microsoft 365 Copilot reverse proxy** — turns your licensed M365 Copilot web session (Substrate SignalR WebSocket) into a production-grade OpenAI / Anthropic compatible API.

> Unofficial. Not affiliated with Microsoft. Uses the same undocumented `substrate.office.com` ChatHub protocol as the M365 Copilot web UI. For personal / self-hosted use with accounts you are authorized to operate. Interfaces can change without notice.

## Features (product target)

| Area | Capability |
|------|------------|
| **Protocol** | Real Substrate WebSocket (`wss://substrate.office.com/m365Copilot/Chathub`) + SignalR JSON |
| **API** | OpenAI `chat/completions` + `models`, Anthropic `messages`, OpenAI Responses (phased) |
| **Models** | Auto-discover / advertise tones (Magic, Quick, Reasoning, Claude_*, Gpt_*, …) |
| **Tools** | Zero pre-registration — dynamic `tools[]` from each client request; multi-agent wire formats |
| **Accounts** | Account pool, health, cooldown, round-robin / sticky; semi-auto import (browser / token paste) |
| **Auth** | Gateway API keys, admin session for WebUI |
| **WebUI** | Dashboard: pool, tokens TTL, request log, model list, import wizard |
| **Ops** | Docker, structured logs, frame dump, `/health` |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[browser,dev]"

# first-time config
cp config.example.yaml config.yaml
# edit gateway.api_keys and data_dir

mcg serve
# API  http://127.0.0.1:8080/v1
# UI   http://127.0.0.1:8080/ui
```

Point any OpenAI-compatible client at:

| Setting | Value |
|---------|--------|
| Base URL | `http://127.0.0.1:8080/v1` |
| API Key | your `gateway.api_keys` entry |
| Model | `m365-copilot` (or see `GET /v1/models`) |

## Architecture

```
Clients (Cursor / Claude Code / OpenCode / Open WebUI / SDK)
        │  OpenAI | Anthropic | Responses
        ▼
┌─────────────────────────────────────┐
│  Gateway API + Auth + WebUI         │
├─────────────────────────────────────┤
│  Compat layer  →  CanonicalRequest  │
│  Tool loop (dynamic tools, no registry) │
│  Multimodal adapter                 │
├─────────────────────────────────────┤
│  Account pool + Token fabric        │
│  (hot cache → disk → CDP refresh)   │
├─────────────────────────────────────┤
│  Substrate client (SignalR / ChatHub) │
└─────────────────────────────────────┘
```

## Code references / attribution

This project is an independent implementation. Protocol understanding and design choices draw heavily from public open-source work. **Study their code; do not violate their licenses when copying.**

| Project | Role in our design | License (as published) |
|---------|-------------------|-------------------------|
| [cramt/m365-copilot-proxy](https://github.com/cramt/m365-copilot-proxy) | Deepest protocol notes (`docs/m365-copilot-api.md`), stream fold, stop frame, throttle, tool fence / shell-routing, native actions | check repo |
| [kuchris/m365-copilot-openai-proxy](https://github.com/kuchris/m365-copilot-openai-proxy) | Clean Python Substrate client skeleton, token refresh notes, OpenAI proxy shape | Apache-2.0 |
| [HEXUXIU/M365-Copilot2API](https://github.com/HEXUXIU/M365-Copilot2API) | Payload optionsSets, conversation history, connection reuse, CLI/setup patterns | Research / check repo |
| [edlaver/m365-copilot-bun-proxy](https://github.com/edlaver/m365-copilot-bun-proxy) | Dual Graph+Substrate client patterns, session store, debug logging | check repo |
| [nizarfadlan/m365-copilot-proxy](https://github.com/nizarfadlan/m365-copilot-proxy) | Rust port of kuchris; Metrics frame; CDP browser attach ideas | Apache-2.0 |

**Not used as Substrate base (different product line):**

- GitHub Copilot proxies (e.g. `ericc-ch/copilot-api`)
- Consumer `copilot.microsoft.com` proxies (e.g. `sums001/Windows-Copilot-API`)
- Browser-fetch-only gateways without ChatHub (e.g. parts of `iv0rish/m365-copilot-proxy`)

See [docs/ATTRIBUTIONS.md](docs/ATTRIBUTIONS.md) and [docs/protocol.md](docs/protocol.md).

## Agent client compatibility

Tools are taken **only from the inbound request** — no server-side tool registration required.

| Client | Wire | Status |
|--------|------|--------|
| OpenAI SDK / Open WebUI | Chat Completions + `tools` | P0 |
| Cursor / Continue / Cline | OpenAI-compatible | P0 |
| Claude Code | Anthropic Messages + tools | P1 |
| OpenCode / Codex-style | OpenAI Responses | P1 |
| Custom agents | Any of the above | map via compat layer |

Tool execution: **client-executed by default** (gateway emits `tool_calls`; client runs tools and posts results). Optional local executors are off by default.

## Auth (mature — cramt/lezi)

```bash
pip install -e ".[auth]"   # msal
mcg login --label alice    # print PKCE URL
mcg login --id KEY --finish "…/oauth2/nativeclient?code=…"
mcg refresh-token ACCOUNT  # silent MSAL / sidecar RT
# fallback
echo "$JWT" | mcg import-token - --label alice
```

See [docs/TOKEN_CDP.md](docs/TOKEN_CDP.md).


## Security

- Default bind: `127.0.0.1`
- Require gateway API key for `/v1/*`
- Admin WebUI cookie / password separate from API keys
- Tokens stored under `data_dir` with restricted permissions
- Do not expose publicly without TLS + strong keys + network policy

## API surface

| Endpoint | Notes |
|----------|--------|
| `POST /v1/chat/completions` | OpenAI chat (+ tools, multimodal content parts) |
| `POST /v1/messages` | Anthropic Messages API shape |
| `GET /v1/models` | Advertised model / tone list |
| `GET /v1/models/probe` | Catalog snapshot (no live calls) |
| `POST /v1/models/probe?max_tones=2` | Live tone probe (uses quota) |
| `GET /health` | Health + feature flags |
| `GET /admin/auth/status` | Token / refresh / keepalive |

### Tools execution

```yaml
tools:
  execution: client   # default-safe: return tool_calls to client
  # execution: local  # gateway runs shell-like tools (bash/shell/…)
  max_rounds: 8
```

### Multimodal

OpenAI `image_url` / `input_audio` parts are parsed, hashed, injected into the
Substrate text prompt, and best-effort attached as `imageBase64` / `imageUrl`
on the chat message (tenant may ignore unknown fields).

## Development status

| Phase | Scope | State |
|-------|--------|--------|
| P0 | Chat + OpenAI + Anthropic + tools + multimodal + probe + auth keepalive | **done** |
| P1 | Account pool + token fabric + WebUI shell | done (basic) |
| P2 | Dynamic tools multi-agent loop (client + local) | done |
| P3 | Model list + live probe | done |
| P4 | Multimodal adapters | done (text+extras path) |
| P5 | Hardening, Docker, frame dump, docs polish | planned |

## License

Apache-2.0 for original code in this repository.  
Third-party protocol knowledge remains subject to upstream project licenses and Microsoft terms of use.
