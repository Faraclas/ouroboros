# Project Work Items — bundled backend

Source-only built-in capability. Normal source admission, grants and enablement apply; source presence is not installed-consumer verification. Existing external copies and their private SQLite state are not migrated or replaced.

## Files and state

- `SKILL.md`: parent's manifest-first declaration, with usage notes added below.
- `plugin.py`: PluginAPI 2.0 tool and HTTP adapters, immutable host-derived agent attribution, honest `via Interface/API` attribution.
- `board_store.py`: stdlib SQLite canonical state and validation; no execution readers.
- `test_boards.py`: actual SQLite, spawn-process, fresh-process and ASGI HTTP tests.
- `test_host_boundary.py`: actual installed ABI/staged registration/strict registry/root-selection checks with temporary state only.

Runtime storage is exactly `Path(api.get_state_dir()) / 'boards.sqlite3'`. `ctx.drive_root` never selects it. Runtime project existence uses `api.get_runtime_info()['data_dir']` and installed `ouroboros.projects_registry.get_reserved_project(..., strict=True)`, irrespective of lifecycle. This is a deliberate narrow internal-host dependency, like the existing project-orientation skill, rather than a second registry parser or unauthenticated loopback fetch. Unknown projects return 404; registry/storage unavailability 503, not false absence. Closing/reopening doesn't delete items. No delete endpoint exists.

One connection per operation; SQLite WAL/FULL-default durability and `BEGIN IMMEDIATE` mutation transactions. Item change, prerequisite changes/comment and request receipt commit together. Reads use one SQLite snapshot. First-process journal-mode upgrade genuinely raced in testing; pretransaction SQLITE_BUSY/LOCKED setup now closes/reopens under the existing 15-second database wait allowance. It never retries a mutation. Other storage failures return typed unavailable with no manufactured effect.

## HTTP seam

All paths beneath `/api/extensions/project-boards/`. Success is `{ok:true,...}`; failure `{ok:false,error:{code,message},current?:item}`. All responses are `Cache-Control: no-store`. The host's existing network authentication/liveness/grant/method boundaries remain; a route has no ToolContext and must never claim a named human or agent identity. It can be called by whatever callers the configured host API already authorizes.

- GET `list`: `project_id` required; optional `q`, `disposition`, `archived=false`, `offset=0`, `limit=100` (1..200). Offset is a nonnegative SQLite signed-64-bit integer. Repeated/unknown query keys and invalid types fail 400, not silent coercion. Items ordered oldest creation then ID; client groups/reorders presentation. Returns items, `counts:{idea,planned,done,set_aside}`, filtered `total`, offset/limit/has_more. Counts always cover all nonarchived home-project items, separate from filters. Literal Unicode casefold search includes title/description/outcome/comments. Full summary source text is preserved; preview is last comment else outcome else description, without silent clipping, plus comment_count.
- GET `item?item_id=...`: full item, chronological comments and `needed_by`. Prerequisites add current board target project/title/disposition and explicit missing marker. No task evidence is resolved.
- POST `capture`: `{project_id,title,description?:'',request_id}` creates a UUID Idea/revision1.
- POST `update`: `{item_id,expected_revision,request_id,changes:{...}}`; allowed title/description/outcome/disposition/archived/references/prerequisites only. Omitted fields survive; empty text/arrays clear; blank title invalid. CAS conflict409 returns current item while preserving caller's unsent draft externally. Item home project is immutable. No transition rules between dispositions.
- POST `comment`: `{item_id,text,request_id,quote_comment_id?:null}`. Appends independently of expected_revision and increments item revision. Quote must be an existing same-item comment; snapshot preserves its ID/text/author/timestamp. No comment editing/deletion.
- GET `export?project_id=...`: `{ok:true,schema_version:1,project_id,exported_at,items:[full item with comments]}` including archived, exact stored newlines/Unicode, author/order/reference/prerequisite data. Cross-project targets remain references, not copied entire boards or task records.

References: `{kind:'task'|'row'|'url',value,label?:''}`. HTTP(S) URL only, no embedded credentials; clients must still use safe text rendering and link handling. Task/row values are opaque, not trusted instructions or resolved status. Prerequisites `{item_id,needs?:''}` validate existing board targets, disallow self/duplicate links, but allow cycles because this isn't a scheduler. Reverse links are derived, not a second store.

Mutations require a nonblank <=200-character request ID. Use a fresh UUID for a NEW intended change. Each ID globally binds operation, exact JSON argument structure and actual actor; changed use returns409 `request_conflict`. Exact retries return ORIGINAL successful response with `duplicate:true`, even if the item changed since; GET item afterwards to refresh. Failed writes create no successful receipt. No arbitrary receipt expiry can silently break that promise. Comments/text preserve exact CRLF/bare CR/Unicode; lone surrogates, nonfinite JSON/deep-invalid JSON and duplicate object keys return400. No controller, network request, model call, chat send, background subscriber, schedule or task lookup exists in this payload.

## Tools and real caller limits

`board_find/get/capture/update/comment` match the same projections. Only find defaults missing project_id to REAL ctx.project_id; explicit cross-project targets work. Author is `{surface:'agent',label:'Ouroboros',task_id,source_project_id?,chat_id?}` from frozen ToolContext scalar fields, never caller kwargs or raw metadata. HTTP author is exactly `{surface:'interface',label:'via Interface/API'}`.

Manifest effects are read/read/write/write/write. Existing host Presence exact-tool grants, Advises/Silent/Paused checks, disabled-tools/Observe/network/resource/Safety rules are not replaced by a same-project or non-owner ban. Because enabled external tools can otherwise reach read-only children, mutation handlers reuse actual `ouroboros.tool_access.active_tool_profile(ctx)` and refuse `local_readonly_subagent` with403/typed `readonly`; reads remain available. Root and properly constrained acting actors can mutate foreign project boards. This preserves an existing assignment restriction, not a new project whitelist.

### Exact isolated-process host limitation

Installed `extension_process_runner.py:216–255` serializes/applies task_metadata/task_contract but not `ctx.task_constraint`; `_call_tool:1008–1010` starts a fresh bare ToolContext. `tool_access_roots.py:63–96` sees delegated role before frozen acting profile and therefore conservatively resolves an isolated acting child as read-only when its concrete acting constraint was lost. Our test pins that refusal and separately pins in-process acting success. We do NOT reconstruct missing authority or edit core. Parent must report/find/file this owning-host issue under its actual publication authority if relevant.

This payload requests no isolated dependencies/native marker. Parent must still establish ACTUAL installed execution mode from host descriptors/dispatch, not infer in-process merely from this sentence. The installed actual root + explicitly granted acting-child tool → HTTP shared canonical DB proof remains the parent's acceptance step. Staged API tests and mocked process-spawn leaf are NOT that installed proof.

## Verification and portability

Run `python3 -B verify_backend.py --host-source /path/to/checkout --evidence-dir /path/to/task-evidence`.
The verifier explicitly selects host source and writes evidence outside the payload.
Tests exercise temporary SQLite state, HTTP adapters, host registration and caller
profiles. These are not a claim about a particular installed process or platform.
The runtime uses the host's strict project reader and actual read-only-profile
predicate; registration validates both before exposing surfaces. No isolated
runtime, credentials, execution controller, network client or migration is added.

List pagination bounds item count, not total text bytes; discussion and exports
retain full content. Very large boards can therefore be costly. Future paging
must preserve full-text access rather than silently truncate.
