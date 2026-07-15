# Token acquisition — what actually works

## Live probe results (2026-07-15, this repo)

We hit `login.microsoftonline.com` from CI/sandbox **without completing interactive MFA**.

| Step | Result |
|------|--------|
| Device code **START** with random/custom `client_id` | **Fail** `unauthorized_client` |
| Device code START with first-party ids (Office `d3590ed6-…`, Azure CLI, …) + substrate-like scope | **HTTP 200** + `user_code` |
| Device code **POLL** without user finishing `login.microsoft.com/device` | Always `authorization_pending` — **no AT/RT** |
| `grant_type=refresh_token` with fake RT | `invalid_grant` |
| Full user complete → AT with `aud=https://substrate.office.com/...` accepted by ChatHub | **Not proven here** (needs real MFA + Bizchat accept). Community reverse-proxies almost always use **browser-captured JWT**, not device-code OAuth. |

### Hard truths

1. **Device code can start** for some Microsoft first-party public clients, but that is **not** the same as “you get a legal long-lived refresh_token that ChatHub accepts.”
2. **Your own Entra app** cannot simply declare `https://substrate.office.com/ows/.default` the way you do for Graph. Substrate is a first-party resource; third-party apps typically get `invalid_scope` / consent failures.
3. **First-party client_id reuse** (Office/Azure CLI) for automation is fragile, against Microsoft ToS, and may yield tokens whose `aud`/claims still fail Bizchat/ChatHub.
4. **What production gateways actually use today**
   - **L1:** paste / CDP-sniff `access_token` from browser ChatHub WS (TTL ~1h)
   - **Optional:** keep browser session cookies / CDP re-open to mint a new AT
   - **Not reliable as default:** pure device-code → refresh_token → silent forever for Substrate Copilot

## Recommended default (honest)

```bash
# Still the reliable path
echo "$JWT" | mcg import-token - --label alice
# re-paste when TTL low, or use optional CDP on a machine that has a browser
```

```yaml
token:
  prefer_cdp: false
  # oauth_* is experimental / only if YOU verified AT aud against ChatHub
  oauth_client_id: null
```

## Experimental OAuth (disabled claims)

`mcg device-login` / `refresh_token` storage remain in the tree for operators who **bring their own working client_id + proven substrate AT**.  
Gateway will **not** advertise them as “works out of the box.”

See probe script: `scripts/probe_oauth_device.py`.
