# Copilot credits — bundled extension

A read-only Ouroboros extension that serves the owner's GitHub Copilot
**premium credit usage** to the Viper header:

```
GET /api/extensions/copilot-credits/usage   ->  HTTP 200 JSON, schema_version 1 (API.md)
```

It reads one fixed upstream endpoint, `GET https://api.github.com/copilot_internal/user`,
and returns a sanitized projection of its `quota_snapshots.premium_interactions` bucket.
It registers no tools, adds no model context, runs no timers, daemons or companions,
and writes nothing. `API.md` is the frozen contract shared with the UI
(`copilot_usage.js`); examples live in `examples/`.

## Files

| file | purpose |
|---|---|
| `SKILL.md` | manifest (`type: extension`, `permissions: [net, route, read_settings]`) |
| `plugin.py` | the whole extension (`register(api)`) |
| `API.md` | response contract, error codes, timing |
| `examples/{fresh,stale,unavailable}.json` | generated example answers |
| `tests/test_plugin.py` | offline stdlib tests; also the example generator |

## Credential and origin binding

- The credential is the **current granted** `OPENAI_COMPATIBLE_API_KEY`, read through
  `api.get_settings` on **every** request (the host returns only currently granted,
  allowlisted keys, `{}` otherwise). Nothing is cached across requests except a salted
  HMAC fingerprint.
- It is sent to GitHub **only** when the paired `OPENAI_COMPATIBLE_BASE_URL` is an exact
  GitHub Copilot HTTPS origin: `api.githubcopilot.com`, `api.individual.githubcopilot.com`,
  `api.business.githubcopilot.com`, `api.enterprise.githubcopilot.com` (scheme `https`,
  no userinfo, no query/fragment, port absent or 443; a path such as `/v1` is ignored).
  Anything else — `https://api.openai.com`, `http://…`, `user@host`, `:8443`, `?x=1`,
  lookalikes such as `api.business.githubcopilot.com.evil.com` — answers
  `credential_origin_mismatch` and **no request is sent**. This keeps a different
  vendor's key from ever reaching GitHub.
- No fallback: `OPENAI_API_KEY`, `GITHUB_TOKEN` or any other setting is never read.
- A key that is not 1..1024 printable ASCII characters (e.g. contains CR/LF or spaces)
  answers `credential_unusable` without a request, so it can never inject headers.

## Cache, single-flight, backoff and failure rationale

- **One shared in-memory cache** keyed by `HMAC-SHA256(per-process random salt,
  origin + key)`. A successful read is reused for **60 s**; the UI polls no faster.
- **Single-flight:** concurrent requests share one upstream call; followers wait at most
  12 s and then get the cached answer (`pending`). Ten simultaneous polls → one GitHub call.
- **Backoff:** any failure blocks new upstream calls for 60 s; 401/403 for 300 s;
  429 honours a numeric `Retry-After` clamped to 60..900 s (default 300 s when absent or
  an HTTP-date). Earlier polls get the cached answer and `next_refresh_after`.
- **Stale means same credential:** after a transient failure (timeout, network, 404, 5xx,
  bad JSON…) the last good snapshot of the **same** credential is shown as `stale` with
  the error, for at most 24 h. A key rotation, base-URL change, removal, wrong origin, or a
  401/403 drops the snapshot immediately, so data from one credential is never shown
  under another, and a revoked credential's numbers do not linger.
- **Credential change during a refresh:** before publishing, the refreshing request
  re-reads the current credential. If its fingerprint differs from the one the request
  was made with (rotated, re-pointed or removed), the result is discarded, the cache is
  reset and the answer is `unavailable` / `credential_changed`; waiting followers get the
  same. The next poll uses the new credential.
- **Transport:** fixed host `api.github.com`, fixed path, GET only, one attempt, no
  redirects (`http.client` never follows them; any 3xx → `redirect_refused`), body read
  only for 200 and capped at 512 KiB (by `Content-Length` and while streaming).
- **Time bound, stated honestly:** the 10 s budget is a *socket-level per-operation
  timeout with remaining-budget tightening*. The connection starts with a 10 s timeout
  (TCP connect + TLS); after connect, before `getresponse` and before every body read
  the socket timeout is lowered to the remaining budget, and an exhausted budget answers
  `timeout` between phases (if connect alone uses it up, the token is never sent). A peer
  trickling bytes within one operation, several resolved addresses or a slow TLS handshake
  can still stretch wall time past 10 s, and **DNS resolution is not bounded by the
  extension** (no extra threads or alarms are used). The host runs the sync handler via
  `asyncio.to_thread`, so a slow read occupies a worker thread, not the event loop.

## Sanitization

- Every answer is HTTP 200 JSON with `Cache-Control: no-store`, also on failure; the
  route handler catches everything (host error JSON would otherwise echo exception text)
  and answers `internal`.
- `error.message` is a fixed English sentence per code; provider bodies, exception text,
  headers and the token never appear in any answer or log line (the log carries only the
  error code).
- Only allowlisted fields are projected: `login` must look like a GitHub login, `plan` a
  short identifier, dates must parse as ISO-8601 and are normalized to UTC `Z`; anything
  else becomes `null`. Numbers must be finite, non-boolean JSON numbers.
- Percentages come only from the provider's `percent_remaining` (0..100, never clamped);
  a missing/invalid value or `entitlement == 0` on a limited plan is
  `unrecognized_response`, never an invented number. Unlimited plans report `null`
  percentages (show "Unlimited").

## Money and counters

- **`nominal_usd` is a consumption value, not an invoice.** It uses
  `1 credit = $0.01` and appears only when `token_based_billing === true`. Actual
  billing (overage, discounts, seat pricing, org policies) is not reflected.
- **`credits_used` vs percentage:** `used_percentage = 100 − percent_remaining` comes from
  the provider's own quota math. `credits_used` is a separate provider counter that does
  not share that denominator (e.g. the fixture shows 3 100 credits used of a 10 000
  entitlement, yet 75.8 % used); never derive a percentage from `credits_used /
  entitlement` or show the two as one ratio.
- The endpoint is GitHub's internal Copilot endpoint (`copilot_internal`); its shape may
  change without notice, in which case the extension answers `unrecognized_response`.

## Tests and examples

```
cd skills/copilot-credits
python3 tests/test_plugin.py                    # run the offline suite
python3 tests/test_plugin.py --write-examples   # regenerate examples/*.json
```

The tests are stdlib `unittest`, fully offline (fake fetch, clocks starting at 0,
settings, `HTTPSConnection` and plugin API; real sockets are blocked). The examples are
generated from the synthetic fixture with base URL
`https://api.business.githubcopilot.com`, and a test fails when they are out of date.

## Admission and use

This source ships in the repository's built-in catalog. Fresh onboarding may
select it as a recommended starter; an existing external same-name copy remains
untouched. Existing installations are not automatically offered a replacement.
Normal built-in source trust, review, auto-grant and enablement settings apply.
No credentials, grants or personal quota observations are bundled. The examples
use a synthetic account and counters, including deliberately disagreeing counters.

Grant the two manifest-requested settings only through the host's normal grant
flow and enable the skill. With no suitable credential it returns an honest
unavailable response without an upstream request. A route missing because the
skill is disabled or not admitted is distinct from a provider failure.
