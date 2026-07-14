# Substrate ChatHub protocol (summary)

Independent notes for implementers. Authoritative community write-ups:

- https://github.com/cramt/m365-copilot-proxy/blob/main/docs/m365-copilot-api.md
- kuchris / HEXUXIU / edlaver Substrate clients

## Endpoint

```
wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}
  ?ClientRequestId=...
  &X-SessionId=...
  &ConversationId=...
  &access_token=<JWT aud=https://substrate.office.com/...>
  &variants=...
  &source=officeweb&product=Office&agentHost=Bizchat.FullScreen
  &licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot
```

Header: `Origin: https://m365.cloud.microsoft`

## Frames (SignalR JSON, RS = 0x1E)

1. Client: `{"protocol":"json","version":1}\x1e`
2. Server: handshake ack
3. Client: type `4` invocation `target=chat` with arguments (message, optionsSets, tone, …)
4. Server: type `1` `target=update` with `writeAtCursor` and/or `messages[]`
5. Server: type `3` completion
6. type `6` ping — ignore
7. Optional stop: type `1` `target=stop`

## Streaming caveat

Deltas and full-text snapshots interleave. Use prefix-safe fold (see `fold_stream_text`).

## Tools

ChatHub is **not** OpenAI tools. Gateway injects ephemeral tool instructions and parses model text into `tool_calls` for agent clients.
