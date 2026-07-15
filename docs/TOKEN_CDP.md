# Token acquisition — CDP semi-auto

## Paths (fastest first)

| Level | Source | When |
|-------|--------|------|
| L0 | Memory hot cache | Every request |
| L1 | `data/tokens/<oid>.jwt` | Restart |
| L2 | **CDP browser capture** | Near expiry / missing / first login |
| L3 | Manual paste | Fallback |

## CLI

```bash
# First login (opens dedicated browser profile under data/browser-profiles/)
mcg browser-login --label alice

# Attach already-running Chrome with --remote-debugging-port=9222
mcg browser-login --cdp http://127.0.0.1:9222 --label alice

# Refresh existing account using saved profile cookies
mcg refresh-token <oid>
```

## WebUI

Admin login → **Start browser capture** (blocks until JWT or timeout).

## Admin API

```http
POST /admin/accounts/browser-login
Authorization: Bearer <api-key>
Content-Type: application/json

{"admin_password":"...","label":"alice","interactive":true}
```

## Requirements

- Chromium / Chrome / Edge installed (`MCG_BROWSER` or auto-detect)
- Display for first MFA login (or attach remote CDP)
- Isolated `--user-data-dir` — never hijacks your daily browser profile

## Security

- Profiles and JWTs live under `data_dir` with mode 0600 where possible
- Gateway still requires API keys for `/v1`
- Do not expose CDP port (`9222`) to the public internet
