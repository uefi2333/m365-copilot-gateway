# Roadmap

## Done in scaffold (this commit)

- [x] Repo layout, Apache-2.0, README + attributions
- [x] Config YAML
- [x] Substrate protocol builders + streaming client + fold
- [x] Token fabric L0/L1 + JWT validation
- [x] Account pool + import token
- [x] OpenAI `/v1/models` + `/v1/chat/completions` (stream/non-stream)
- [x] Gateway API key auth + admin password
- [x] WebUI dashboard (login, import, list, models, logs)
- [x] Dynamic tool preamble + parse (zero registry)
- [x] CLI: serve / import-token / accounts / models

## Next

- [x] Lightweight tokens: paste JWT + OAuth refresh_token / device-code (HTTP)
- [x] CDP browser-login optional (`prefer_cdp: false` by default)
- [x] Stream path emits tool_calls chunks cleanly (OpenAI delta index shape)
- [ ] Anthropic `/v1/messages`
- [x] Sticky conversationId across turns (`user` / `conversation_id` / account)
- [ ] Live model probe / capability endpoint
- [ ] Image/audio adapters
- [ ] Docker Compose
- [ ] Frame dump CLI
- [ ] Rate-limit aware queue from throttle fields
