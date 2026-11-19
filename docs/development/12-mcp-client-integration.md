# MCP Client Integration

MCP is client-only; remote data is not policy. Owners select Settings references.

`mcp_catalog.py`: atomic/stat-cached `_schema_version: 1` envelopes at `config.DATA_DIR/state/mcp/catalogs/server-<canonical_id>.json` (call-time root). `server_id`, `identity` (SHA-256: config, credentials/env; excludes enablement/allowlists/pin/mode), `listed_at`, `complete`, ordered `items`, `instructions`, `schema_omissions`, `latest_attempt {at,ok,error,partial}`. Items: name, description, input_schema; metadata: title, annotations, outputSchema. Drop secret-bearing defaults/examples/outputSchema with notice; withhold only irreducible input contracts. `normalize_listing`/`_adopt_catalog` disclose live/old omissions; no disk wrapping/cuts.

Boot/MCP Save/mtime/Refresh list tools. Configure seeds; reads adopt files without changing resident request arrays. Serialize per server, no manager lock over network waits; config events coalesce a follow-up pass. Complete success (empty too) replaces evidence. Failure updates only the attempt; partial success keeps complete evidence. First failure is attempt-only; stale results write nothing. Failed publication keeps local tools, reports `publication_error`, never `published`. Only config events prune (`test_mcp_catalog.py`).

SDK2.3–2.x HTTP/SSE validate URLs/headers. Credentials stay origin-bound; even uncredentialed cross-origin redirects now fail under SDK2 (configure the destination URL). Settings masks headers, not echoes. Logs mask tokens/selected secrets, not header values. Loopback proof: `tests/test_mcp_*.py`.

Stdio uses exact argv, cwd and selected env. After admission, `workspace_executor.resolve_process_env` resolves references under `tools/process_facts.py::settings_environment_allowed`: root/Cyber acting, not Presence; MCP/skill grants add no reference authority. Mask secret diagnostics/logs; keep secrets out of host argv, shell source and process ledgers; Docker uses inert aliases. Services overlay a minimal baseline; commands/scripts/verification inherit theirs. Tests: `test_process_environment.py`, `test_workspace_executor_services.py`.
