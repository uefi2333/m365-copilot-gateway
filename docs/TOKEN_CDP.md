# Token acquisition — mature path (cramt / lezi aligned)

## What production reverse-proxies actually use

| Rank | Method | Repo | Auto renew |
|------|--------|------|------------|
| 1 | **MSAL silent** after PKCE / Playwright login | cramt/m365-copilot-proxy | Yes (msal-cache) |
| 2 | **PKCE auth-code** (paste `nativeclient?code=`) | lezi gettoken.py | Seeds cache |
| 3 | **Browser paste** ChatHub `access_token` | lezi / everyone | No (~1h) |
| 4 | CDP sniff WS | optional | Session-dependent |
| ✗ | Custom Entra + `ows/.default` device code | — | Does not yield ChatHub JWT |

## Recipe (verified community, June 2026)

```
client_id:  c0ab8ce9-e9a0-42e7-b064-33d422df41f1
scopes:     https://substrate.office.com/sydney/M365Chat.Read
            https://substrate.office.com/sydney/sydney.readwrite
redirect:   https://login.microsoftonline.com/common/oauth2/nativeclient
aud:        https://substrate.office.com/sydney
```

### CLI

```bash
# 1) Start PKCE
mcg login --label alice
# open printed URL, sign in, copy nativeclient?code= URL (wrongplace page is OK)

# 2) Finish
mcg login --id <account_key> --finish "https://login.microsoftonline.com/common/oauth2/nativeclient?code=..."

# 3) Later renewals (no browser if MSAL/sidecar RT valid)
mcg refresh-token <account_id>

# Fallback: paste browser JWT
echo "$JWT" | mcg import-token - --label alice
```

### nativeclient gotcha

Browser often bounces to `/common/wrongplace`. The `?code=` lives on the
**navigation request** to `.../oauth2/nativeclient?code=...`. Copy that URL
from DevTools Network or the brief address-bar flash.

### Device code

Often rejected for this first-party client. Prefer `mcg login`.

### Live probes (2026-07-15)

- Custom client_id + ows scope → unauthorized_client
- Office client + substrate-like scope → device START may 200 but without MFA no AT/RT
- Mature path uses **PKCE + Sydney scopes**, not ows device code

## CDP (optional)

Only if `prefer_cdp: true` and Chrome is available. Not required for mature MSAL path.
