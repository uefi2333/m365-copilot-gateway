# Attributions & prior art

## Scope

`m365-copilot-gateway` re-implements an OpenAI/Anthropic-compatible gateway over the **Microsoft 365 Copilot Substrate ChatHub** WebSocket. Protocol fields were learned by reading public reverse-engineering projects and documentation, then rewritten in this codebase.

We **do not** claim ownership of Microsoft's APIs or of upstream projects' code.

## Primary references

### 1. cramt/m365-copilot-proxy

- URL: https://github.com/cramt/m365-copilot-proxy
- Why: Most complete public write-up of the undocumented SignalR protocol (`docs/m365-copilot-api.md`); streaming fold rules; stop frame; throttle; Disengaged; fenced tool calling; shell-routing; Copilot Studio / native action experiments.
- Used as: protocol authority and design reference for `src/mcg/substrate/` and `src/mcg/tools/`.

### 2. kuchris/m365-copilot-openai-proxy

- URL: https://github.com/kuchris/m365-copilot-openai-proxy
- License: Apache-2.0
- Why: Readable Python Substrate client (`substrate_client.py`), JWT audience checks, Edge/CDP token capture flow, OpenAI proxy surface.
- Used as: structural template for Python packaging and token validation patterns.

### 3. HEXUXIU/M365-Copilot2API

- URL: https://github.com/HEXUXIU/M365-Copilot2API
- Why: Richer `optionsSets` / variants, conversation `messageHistory`, connection reuse, setup wizard UX ideas.
- Used as: payload and session ergonomics reference.

### 4. edlaver/m365-copilot-bun-proxy

- URL: https://github.com/edlaver/m365-copilot-bun-proxy
- Why: Production-minded Substrate client + session store + debug logging; optional Graph path (we stay on Substrate by default).
- Used as: operational patterns (timeouts, logging, hub URI construction).

### 5. nizarfadlan/m365-copilot-proxy

- URL: https://github.com/nizarfadlan/m365-copilot-proxy
- License: Apache-2.0
- Why: Rust port of kuchris; Metrics frame; multi-browser CDP executable discovery.
- Used as: cross-check for frames and token refresh behavior.

## Explicit non-bases

| Project | Reason |
|---------|--------|
| GitHub Copilot API proxies | Different product / auth |
| Windows / consumer Copilot API | Different endpoints |
| Graph-only Copilot samples | Official Graph path ≠ web ChatHub session |

## Contributing attribution

When porting a non-trivial algorithm from an upstream file:

1. Prefer re-implementation from documented behavior.
2. If a snippet is copied almost verbatim, keep a file-level comment with source URL + license.
3. Never strip upstream copyright headers from copied files.

## Microsoft

Microsoft, Microsoft 365, and Copilot are trademarks of Microsoft Corporation. This project is not endorsed by Microsoft.
