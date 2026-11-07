---
name: project-boards
title: Project Work Items
version: 0.1.1
type: extension
plugin_api: '2.0'
entry: plugin.py
os: any
permissions: [tool, route]
description: Shared project work-item capture, discussion and references; passive context, not an execution controller.
when_to_use: Capture, inspect or maintain an idea, planned outcome, discussion or cross-project prerequisite in a project's Work Items.
model_experience:
  what_model_sees: Read and mutation tools for shared Work Items across projects. Stored dispositions describe authored intent, never actual execution. Use existing workflow tools to inspect linked work.
  token_effect: A small set of tool schemas while enabled; item text is read only when requested.
tools:
  - name: board_find
    effect: read
  - name: board_get
    effect: read
  - name: board_capture
    effect: write
  - name: board_update
    effect: write
  - name: board_comment
    effect: write
---

# Project Work Items

Shared Work Items preserve ideas, intended outcomes, discussion and informational prerequisite links keyed by stable project identity. Title-only capture starts as Idea. Idea, Planned, Done and Set aside are hand-set dispositions, not enforced transitions or live execution states. A completed run does not automatically complete an item.

Saving, commenting, editing or linking never starts, steers, pauses, resumes or reorders agent work. Existing durable inspectable workflow records establish execution reality. The skill stores task/row/HTTP references, not copied task results or live status; the personal client reads existing public workflow APIs under its own session. Other agents use their existing authorized workflow readers.

Agent attribution uses the host's actual ToolContext task/project identity, not author arguments. HTTP mutations are labelled via Interface/API, not a separately authenticated person. Cross-project maintenance is intentional; source project is attribution, not an exclusive permission fence. Existing tool, actor, Presence, Safety and owner controls remain applicable.

Data lives in the extension's private state directory, separately from project metadata and source files. Closing/reopening a project does not remove its items. Removing/uninstalling this skill may remove its state; export the board first. The export route and exact API/verification instructions are documented in BACKEND.md.

## Use and verification

Use `board_find` in an explicit registered project (omitted target defaults to the current project), then `board_get`. Capture with a fresh request UUID, update using the returned revision and a fresh UUID, and append discussion using a fresh UUID without an expected revision. Reuse the EXACT same arguments and UUID after unknown delivery; a duplicate returns the original response, so read the current item afterwards. A409 revision conflict means retain the draft and compare current content, not silently overwrite it.

The six HTTP routes under `/api/extensions/project-boards/` provide list/item/export reads and capture/update/comment writes. The exact JSON seam, SQLite state/exports, limits, internal host dependencies, installed-process limitation and reproducible staged verification are in `BACKEND.md`. Tests: `python3 -B verify_backend.py --host-source /path/to/serving/ouroboros --evidence-dir /path/to/task-evidence` (explicit source and import-origin assertion, never accidentally the separately installed package).

Mutation handlers reuse the installed host's actual read-only profile predicate; enabled external-tool availability must not override an explicit read-only assignment. Existing host Presence grants, owner controls and task restrictions still apply. API routes do not establish agent or human identity and label writes honestly via Interface/API.

Readiness follows the host's current review, grants, enablement and runtime facts; source existence alone proves none of them. Verification writes evidence outside the reviewed payload so rerunning it does not stale its own review. No credentials, network calls, background task, chat ingress or execution-control permission is requested.

Bundled source follows normal built-in source trust, admission, grants and enablement. It does not import an external installation or its state.
