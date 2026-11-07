---
name: copilot-credits
title: Copilot credits
description: Read-only GitHub Copilot premium credit usage for the owner's header (one GET route, no tools).
version: 1.0.1
type: extension
plugin_api: "2.0"
entry: plugin.py
os: any
permissions: [net, route, read_settings]
env_from_settings: [OPENAI_COMPATIBLE_API_KEY, OPENAI_COMPATIBLE_BASE_URL]
timeout_sec: 30
when_to_use: The built-in header or the owner wants the account's current Copilot premium credit usage.
model_experience:
  what_model_sees: Nothing; the skill registers no tools and adds no context. It only serves GET /api/extensions/copilot-credits/usage.
  token_effect: None.
---

# Copilot credits

Serves `GET /api/extensions/copilot-credits/usage`: a sanitized JSON projection
(schema_version 1, full contract in `API.md`) of the owner's own GitHub Copilot
`premium_interactions` quota, read from the fixed endpoint
`https://api.github.com/copilot_internal/user`.

- Credential: current granted `OPENAI_COMPATIBLE_API_KEY`, read on every request,
  sent ONLY when the paired `OPENAI_COMPATIBLE_BASE_URL` is an exact GitHub Copilot
  https origin (`api.githubcopilot.com`, `api.individual|business|enterprise.githubcopilot.com`).
  No other key or fallback is ever read.
- Fixed origin/path, GET only, no redirects, no retries, 512 KiB body cap; 10 s budget as a
  socket-level per-operation timeout with remaining-budget tightening (DNS resolution is
  not bounded by the extension).
- Shared 60 s in-memory cache with single-flight; failures back off 60 s
  (401/403 300 s, 429 Retry-After clamped 60..900 s). Stale = same credential's last
  good snapshot only (max 24 h); credential change/removal/wrong origin/401/403 drops it.
  A credential that changes while a refresh is in flight discards that refresh's result
  (`credential_changed`).
- Always HTTP 200 JSON with fixed sanitized messages; never returns or logs the token
  or raw provider body. Percentages come from the provider's `percent_remaining`, never
  from `credits_used / entitlement`.
- Nominal USD (1 credit = $0.01) is a consumption equivalent, never an invoice.
- No tools, timers, daemons, companions, page modules or writes.

Bundled source follows normal built-in source trust, admission, grants and enablement. It does not import an external installation or its state.
