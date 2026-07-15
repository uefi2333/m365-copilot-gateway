# Quick start

## 1. Install

```bash
git clone https://github.com/uefi2333/m365-copilot-gateway.git
cd m365-copilot-gateway
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

- `gateway.api_keys` — at least one long random string (client Bearer token)
- `gateway.admin_password` — WebUI password
- `gateway.host` / `port` — default `127.0.0.1:8080`

### Docker

```bash
cp config.example.yaml config.yaml   # edit keys first
docker compose up -d --build
# UI  http://127.0.0.1:8080/ui
# API http://127.0.0.1:8080/v1
```

## 2. Run

```bash
mcg serve
# or: mcg serve --host 0.0.0.0 --port 8080
```

- WebUI: `http://127.0.0.1:8080/ui`
- Health: `GET /health`

## 3. Add an account

Need a **Microsoft 365 Copilot–licensed** work/school account.

1. Open WebUI → admin login
2. **PKCE login** (recommended): generate link → browser sign-in → paste `nativeclient?code=…` URL → finish  
   or paste a substrate JWT (`aud` starts with `https://substrate.office.com/`)
3. Account shows **valid** in the pool

CLI alternative:

```bash
mcg login --label me
# open auth URL from data/msal/last_auth_url.txt (do not re-encode via chat apps)
mcg login --id KEY --finish "https://login.microsoftonline.com/common/oauth2/nativeclient?code=..."
```

## 4. Point a client

| Setting | Value |
|---------|--------|
| Base URL | `http://127.0.0.1:8080/v1` |
| API Key | one of `gateway.api_keys` |
| Model | `m365-copilot` or any id from `GET /v1/models` |

```bash
curl http://127.0.0.1:8080/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_API_KEY")
print(client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "hi"}],
))
```

Works with Open WebUI, Cursor, Continue, Cline, Claude Code (Anthropic `/v1/messages`), Codex CLI, etc.

## 5. Common errors

| Code / message | Fix |
|----------------|-----|
| `no_account` | Add token in `/ui` |
| `token_invalid` | Refresh or re-login PKCE |
| `rate_limited` | Slow down or add more accounts |
| `upstream_auth` | License / expired token |
| AADSTS70011 on login | Open auth URL from file, not a chat-mangled paste |

## Security notes

- Default bind is loopback. Expose only behind TLS reverse proxy + strong API keys.
- Tokens live under `data/`. Do not commit `config.yaml` or `data/`.
- Unofficial reverse proxy of Microsoft Substrate; use only accounts you are allowed to operate.
