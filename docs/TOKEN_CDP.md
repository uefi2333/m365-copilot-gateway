# Token acquisition (lightweight first)

## Default path — no Chrome

| Level | Source | Needs |
|-------|--------|--------|
| L0 | Memory | — |
| L1 | `data/tokens/<oid>.jwt` | paste once |
| **L1.5** | **OAuth `refresh_token` (HTTP)** | `oauth_client_id` + stored RT |
| L2 | CDP Chrome (optional) | `prefer_cdp: true` + browser |
| L3 | Manual paste / device-code | phone browser OK |

### 1) Paste access JWT (simplest)

```bash
# DevTools → Network → WS `substrate.office.com` → copy access_token=
echo 'eyJ...' | mcg import-token - --label alice

# optional: also store refresh_token for silent renew
mcg import-token token.jwt --label alice --refresh-token rt.txt
mcg set-refresh-token <oid> rt.txt
```

### 2) Silent renew (HTTP only)

```yaml
# config.yaml
token:
  prefer_cdp: false
  oauth_client_id: "<your-entra-app-client-id>"
  oauth_tenant: "common"   # or your tenant id
  oauth_scope: "https://substrate.office.com/ows/.default offline_access openid profile"
```

```bash
mcg refresh-token <oid>
```

Gateway `fabric.ensure()` uses refresh_token automatically when access JWT is near expiry.

> Your Entra app must be allowed to request the substrate resource. Many personal setups just paste a fresh JWT periodically if app registration is not available.

### 3) Device code (still no local Chrome)

```bash
mcg device-login --label alice
# print verification_uri + user_code → open on phone
```

### 4) Optional CDP (heavy)

```yaml
token:
  prefer_cdp: true
```

```bash
mcg browser-login --label alice
```

Requires Chrome/Edge binary. Not recommended for servers.
