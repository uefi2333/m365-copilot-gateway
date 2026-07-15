# M365 Copilot Gateway

Self-hosted OpenAI‑compatible proxy for Microsoft 365 Copilot (Substrate/Sydney).

Drop‑in replacement for `api.openai.com` — works with **Cherry Studio**, **Open WebUI**, **NextChat**, **AstrBot**, `curl`, and any OpenAI SDK.

```
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"m365-copilot","messages":[{"role":"user","content":"Hello"}]}'
```

---

## Features

| Capability | Status |
|---|---|
| OpenAI `/v1/chat/completions` (stream + non‑stream) | ✅ |
| Anthropic `/v1/messages` | ✅ |
| Tools / Function calling (OpenAI‑style) | ✅ |
| Multi‑modal (image input, audio input) | ✅ |
| Image generation (Substrate `GraphicArt`) | ✅ |
| Token auto‑refresh (MSAL OAuth, no Chrome) | ✅ |
| Account pool (Round‑Robin / Sticky / Least‑load) | ✅ |
| WebUI admin panel (`/ui`) | ✅ |
| Rate limiting (per‑key / per‑IP) | ✅ |
| Docker Compose (single‑command deploy) | ✅ |
| Production TLS (Caddy + Let's Encrypt) | ✅ |

---

## Quick Start

### 1. Get an M365 Copilot subscription

You need an active Microsoft 365 Copilot license (Microsoft 365 Copilot, not the free Bing Chat).  
The gateway uses the same Substrate protocol as `copilot.microsoft.com` — it authenticates with your M365 identity.

### 2. Deploy

**Docker (recommended):**

```bash
git clone https://github.com/your-org/m365-copilot-gateway.git
cd m365-copilot-gateway

cp config.example.yaml config.yaml
# Edit config.yaml — set at least:
#   gateway.api_keys       → your API key(s)
#   gateway.admin_password → WebUI password
#   token section          → your MSAL/OAuth credentials

docker compose up -d --build
open http://127.0.0.1:8080/ui
```

**Bare metal (Python 3.11+):**

```bash
pip install -e .
mcg serve -c config.yaml
```

### 3. Authenticate

Open the WebUI at `/ui`, log in with `admin_password`, and click **Add Account**:

1. Click "Login with Microsoft" — a browser tab will open
2. Sign in with your M365 Copilot‑licensed account
3. Copy the redirect‑URI code back into the WebUI

The gateway uses MSAL under the hood — tokens are refreshed silently in the background.  
(If MSAL fails, the WebUI also supports Chrome CDP‑based token capture.)

### 4. Use it

Point any OpenAI‑compatible client at the gateway:

```
Base URL:  http://your-server:8080/v1
API Key:   <your-api-key>
Model:     m365-copilot
```

---

## Configuration

See `config.example.yaml` for a complete reference. Key sections:

### `gateway`

```yaml
gateway:
  host: "127.0.0.1"         # bind address (0.0.0.0 for Docker)
  port: 8080
  api_keys:                  # list of valid API keys
    - "sk-your-api-key"
  admin_password: "..."      # WebUI login
  cors_origins: []           # CORS for browser frontends
```

### `rate_limit`

```yaml
rate_limit:
  enabled: false             # enable for production
  requests_per_minute: 60    # per API key / IP
  burst: 10                  # burst window
```

### `token`

```yaml
token:
  use_sydney_msal: true      # MSAL OAuth (recommended)
  # OR for legacy flows:
  oauth_client_id: null
  oauth_client_secret: null
  prefer_cdp: false          # Chrome CDP fallback
```

### `tools`

```yaml
tools:
  execution: client          # "client" = tools run on the caller side
  max_rounds: 8              # max model↔tool hops per request
```

### `models`

```yaml
models:
  advertise:
    - id: m365-copilot       # model name sent to clients
      tone: Magic            # Substrate conversation tone
      label: "M365 Copilot"
```

Available tones: `Magic`, `Gpt_Quick`, `Gpt_Reasoning`, `Claude_Sonnet`, `Gpt_Moody`, `Gpt_Balanced`, `Gpt_Precise`, `Gpt_Creative`.

---

## API Endpoints

### `POST /v1/chat/completions` — OpenAI‑compatible chat

Standard OpenAI request/response shape. Supports streaming (`stream: true`).

**Additional response fields:**
- `usage` — token estimates (prompt, completion, total)
- `timing` — `ttft_ms`, `speed_chars_per_sec`, `output_chars`, `elapsed_ms`
- `conversation_id` — Substrate conversation ID (for debugging)

### `POST /v1/messages` — Anthropic‑compatible chat

Thin adapter over the same Substrate backend.

### `GET /v1/models` — Model list

Returns advertised models + runtime‑detected capabilities.

### `GET /v1/models/probe` — Capability catalog

Static + live‑detected feature flags per model.

### `GET /v1/metrics` — Performance metrics

Recent (up to 50) request timings with TTFT/speed summary.

### `GET /health` — Health check

```json
{"ok": true, "version": "0.2.0", "accounts_total": 1, ...}
```

---

## Production Deployment

### With TLS (Caddy + Let's Encrypt)

1. Set `copilot.uefi233.bond` in `Caddyfile` to your actual domain
2. Ensure ports 80/443 are reachable from the internet
3. DNS must point to your server

```bash
docker compose --profile tls up -d
```

### Behind a reverse proxy

The gateway emits `X-Accel-Buffering: no` for streaming responses — nginx passes SSE through correctly without buffering.

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    chunked_transfer_encoding on;
    proxy_buffering off;
}
```

---

## Architecture

```
┌─────────────────────┐     OpenAI/Anthropic API      ┌──────────────────────┐
│  Client (Cherry     │ ──────────────────────────────▶│  M365 Copilot Gateway │
│  Studio, Open       │                                │  :8080               │
│  WebUI, curl, …)    │◀──────────────────────────────│                      │
└─────────────────────┘     SSE streaming + tools      │  ┌────────────────┐  │
                                                       │  │ Account Pool   │  │
                                                       │  │ (Round‑Robin)  │  │
                                                       │  └───────┬────────┘  │
                                                       │          │           │
                                                       │  ┌───────▼────────┐  │
                                                       │  │ Token Fabric   │  │
                                                       │  │ (MSAL / CDP)   │  │
                                                       │  └───────┬────────┘  │
                                                       │          │           │
                                                       │  ┌───────▼────────┐  │
                                                       │  │ Substrate WS   │  │
                                                       │  │ (SignalR)      │  │
                                                       │  └────────────────┘  │
                                                       └──────────────────────┘
                                                                │
                                                       ┌────────▼────────┐
                                                       │  Microsoft 365   │
                                                       │  Copilot (cloud) │
                                                       └─────────────────┘
```

---

## Development

```bash
git clone https://github.com/your-org/m365-copilot-gateway.git
cd m365-copilot-gateway
pip install -e ".[dev]"
mcg serve -c config.yaml
```

Run tests:

```bash
pytest tests/
```

---

## License

MIT License — see [LICENSE](LICENSE)

---

## Disclaimer

This project is not affiliated with or endorsed by Microsoft Corporation.  
Microsoft 365 Copilot is a trademark of Microsoft Corporation.

Use at your own risk — ensure compliance with your Microsoft 365 subscription terms.
