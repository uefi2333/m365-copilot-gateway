# Roadmap

## Done

- [x] Repo layout, Apache-2.0, README + attributions
- [x] Config YAML
- [x] Substrate protocol builders + streaming client + fold
- [x] Token fabric L0/L1 + JWT validation
- [x] Account pool + import token
- [x] OpenAI `/v1/models` + `/v1/chat/completions` (stream/non-stream)
- [x] Gateway API key auth + admin password
- [x] WebUI dashboard (login, import, list, models, logs)
- [x] Dynamic tool preamble + parse (zero registry)
- [x] CLI: serve / import-token / accounts / models / login / refresh
- [x] Lightweight tokens: paste JWT + Sydney MSAL PKCE
- [x] CDP browser-login optional (`prefer_cdp: false` by default)
- [x] Stream path emits tool_calls chunks cleanly (OpenAI delta index shape)
- [x] Sticky conversationId across turns
- [x] Multi-turn tool result feed (client loop)
- [x] Silent keepalive refresh (MSAL + sidecar RT)
- [x] Light PKCE assist (`mcg login --assist`)
- [x] Anthropic `/v1/messages`
- [x] Live model probe / capability endpoint (`GET|POST /v1/models/probe`)
- [x] Image/audio adapters (prompt + best-effort message extras)
- [x] Server-side local tool execution (`tools.execution: local`)

## Next (P1+)

- [x] Docker Compose + healthcheck
- [x] Actionable API errors (`code` + `hint`)
- [x] WebUI first-run + client snippet panel
- [x] QUICKSTART.md
- [ ] Frame dump CLI
- [ ] Rate-limit aware queue from throttle fields
- [ ] Stronger native multimodal protocol (tenant file upload / ODB)
- [ ] Metrics / Prometheus
- [ ] Production TLS + reverse-proxy examples
