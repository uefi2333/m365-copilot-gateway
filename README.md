# M365 Copilot Pool Core

Minimal core after rebuild.

Kept:

- account pool
- token refresh / PKCE login helpers
- keepalive
- model catalog
- `/v1/models`
- admin account APIs

Removed:

- OpenAI chat proxy
- Anthropic proxy
- tool calling
- multimodal
- WebUI
- agent adapters
- reasoning adapters

## Run

```bash
mcg -c config.yaml serve
```

## APIs

```text
GET /health
GET /v1/models
GET /models
GET /admin/accounts
GET /admin/auth/status
POST /admin/auth/pkce/start
POST /admin/auth/pkce/finish
POST /admin/accounts/import-token
POST /admin/accounts/{account_id}/refresh
POST /admin/auth/keepalive/tick
```
