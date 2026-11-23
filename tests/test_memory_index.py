"""Tests for WI-2.1: derived rebuildable FTS5 memory index (`ouroboros/memory_index.py`),
`memory_read(query=...)` & `closure_brief:<id>` integration, maintenance job, and telemetry.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import time

import pytest

from ouroboros import maintenance, memory_index
from ouroboros.chronicle_store import ChronicleStore
from ouroboros.tools.chronicle import _memory_read, chronicle_tools
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _ctx(root: pathlib.Path, *, task_id: str = "root0001", chat_id: int = 1,
         delegation_role: str = "root", project_id: str = "",
         presence: bool = False) -> ToolContext:
    meta: dict = {"delegation_role": delegation_role}
    if delegation_role == "root":
        meta["root_task_id"] = task_id
    if presence:
        meta["_presence_turn"] = True
    return ToolContext(
        repo_dir=root, drive_root=root, task_id=task_id, current_chat_id=chat_id,
        project_id=project_id, task_metadata=meta,
    )


def _mind(task_id: str = "root0001", room_id: str = "1") -> dict:
    return {
        "kind": "mind",
        "task_id": task_id,
        "focus": {
            "role": "root",
            "task_id": task_id,
            "parent_task_id": "",
            "root_task_id": task_id,
            "chat_id": int(room_id) if room_id.isdigit() else 1,
            "room_id": room_id,
        },
        "route": "unknown",
    }


def _helper(task_id: str = "kid00001", room_id: str = "1") -> dict:
    return {
        "kind": "helper",
        "task_id": task_id,
        "focus": {"role": "child", "task_id": task_id, "room_id": room_id},
        "route": "unknown",
    }


def _write_projects_registry(root: pathlib.Path, projects: list) -> None:
    p = root / "state" / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_schema_version": 2, "projects": projects}), encoding="utf-8")


# --- 1. Knowledge notes (global & project, active & archived) ------------------------------------


def test_indexes_global_and_project_knowledge_excluding_index_full(tmp_path):
    g_dir = tmp_path / "memory" / "knowledge"
    (g_dir / "nested").mkdir(parents=True)
    (g_dir / "index-full.md").write_text("# Index\nindex_only_sentinel_token\n", encoding="utf-8")
    (g_dir / "elias.md").write_text(
        "---\ntitle: Elias Profile\nsummary: Primary human collaborator\nupdated: 2026-11-22\n---\n"
        "Elias works on Panther Zen5 Gentoo workstation.\n",
        encoding="utf-8",
    )
    (g_dir / "nested" / "old-spec.md").write_text(
        "---\ntitle: Archived Spec\narchive:\n  at: '2026-10-01T00:00:00Z'\n  reason: superseded\n---\n"
        "Legacy Panther specification body.\n",
        encoding="utf-8",
    )

    p_dir = tmp_path / "projects" / "alpha" / "knowledge"
    p_dir.mkdir(parents=True)
    (p_dir / "index-full.md").write_text("# Proj Index\nindex_only_sentinel_token\n", encoding="utf-8")
    (p_dir / "design.md").write_text(
        "---\ntitle: Alpha Design\nupdated: 2026-11-23\n---\n"
        "Panther memory architecture design notes.\n",
        encoding="utf-8",
    )

    # index-full.md must never be indexed; first search builds the index cleanly.
    first = memory_index.search_memory(tmp_path, "index_only_sentinel_token", include_archived=True)
    assert first.rebuilt is True and first.hit_count == 0

    # Default excludes archived notes; second search uses the existing index.
    res = memory_index.search_memory(tmp_path, "Panther")
    assert res.rebuilt is False
    assert {h.canonical_address for h in res.hits} == {
        "knowledge_read(topic='elias', scope='global')",
        "knowledge_read(topic='design', scope='project:alpha')",
    }
    assert all(h.status == "ACTIVE" for h in res.hits)

    # include_archived=True surfaces archived note with ARCHIVED status.
    res_all = memory_index.search_memory(tmp_path, "Panther", include_archived=True)
    by_addr = {h.canonical_address: h for h in res_all.hits}
    archived_addr = "knowledge_read(topic='nested/old-spec', scope='global')"
    assert archived_addr in by_addr
    assert by_addr[archived_addr].status == "ARCHIVED"
    assert by_addr[archived_addr].scope == "global"
    assert by_addr[archived_addr].date_ymd == "2026-10-01"

    # Scope filter restricts cleanly.
    res_proj = memory_index.search_memory(tmp_path, "Panther", scope="project:alpha")
    assert [h.canonical_address for h in res_proj.hits] == ["knowledge_read(topic='design', scope='project:alpha')"]


# --- 2. Chronicle records (corrections, folds, decisions, selections, marks) ---------------------


def test_indexes_chronicle_records_and_lifecycle_transitions(tmp_path):
    store = ChronicleStore(tmp_path)
    mind = _mind()
    helper = _helper()

    # Publish two pages, fold them into a part.
    p1 = store.publish_page(
        room_id="1", text="First quantum lattice page.",
        covers={"room_id": "1", "rows": ["r1"], "stream_span": [1, 1]}, author=mind,
    ).record
    p2 = store.publish_page(
        room_id="1", text="Second quantum lattice page.",
        covers={"room_id": "1", "rows": ["r2"], "stream_span": [2, 2]}, author=mind,
    ).record
    part = store.publish_part(
        room_id="1", text="Folded quantum lattice chapter.",
        member_ids=[p1["id"], p2["id"]], author=mind, expected_sequence=store.room_head("1"),
    ).record

    # Publish a helper draft page and reject it.
    draft = store.publish_page(
        room_id="1", text="Draft quantum hallucination.",
        covers={"room_id": "1", "rows": ["r3"], "stream_span": [3, 3]}, author=helper,
    ).record
    assert store.decide(draft["id"], False, mind, "inaccurate draft").ok

    # Publish a note and correct it -> corrected text must be searchable.
    note = store.write_note(room_id="42", task_id="t99", text="Initial note text.", author=mind).record
    assert store.correct(note["id"], "Correction adds quantum_fix_token.", mind).ok

    # Publish global mark + room mark, then release room mark.
    g_mark = store.mark({"kind": "task", "task_id": "t1"}, "Global quantum rule.", mind, room_id="1", scope="global").record
    r_mark = store.mark({"kind": "task", "task_id": "t2"}, "Room quantum reminder.", mind, room_id="42", scope="room").record
    assert store.release_mark(r_mark["id"], mind, "no longer applies").ok

    # Publish two accounts + selections: acc1 replaces part (shown=True), acc2 withdrawn (shown=False).
    acc1 = store.publish_account(
        room_id="1", text="Account one quantum synthesis.", sources=[part["id"]], author=mind,
    ).record
    assert store.select_account(acc1["id"], replaces=[part["id"]], author=mind, reason="common story", shown=True).ok

    acc2 = store.publish_account(
        room_id="1", text="Account two withdrawn quantum draft.", sources=[note["id"]], author=mind,
    ).record
    assert store.select_account(acc2["id"], replaces=[], author=mind, reason="withdrawn", shown=False).ok

    # Search with include_archived=True to verify all statuses & scopes.
    res_all = memory_index.search_memory(tmp_path, "quantum", include_archived=True, limit=50)
    by_addr = {h.canonical_address: h for h in res_all.hits}

    assert by_addr[f"memory_read(node_id='{p1['id']}')"].status == "FOLDED"
    assert by_addr[f"memory_read(node_id='{p1['id']}')"].scope == "room:1"
    assert by_addr[f"memory_read(node_id='{part['id']}')"].status == "SUPERSEDED"
    assert by_addr[f"memory_read(node_id='{draft['id']}')"].status == "REJECTED"
    assert by_addr[f"memory_read(node_id='{g_mark['id']}')"].status == "ACTIVE"
    assert by_addr[f"memory_read(node_id='{g_mark['id']}')"].scope == "global"
    assert by_addr[f"memory_read(node_id='{r_mark['id']}')"].status == "RELEASED"
    assert by_addr[f"memory_read(node_id='{r_mark['id']}')"].scope == "room:42"
    assert by_addr[f"memory_read(node_id='{acc1['id']}')"].status == "ACTIVE"
    assert by_addr[f"memory_read(node_id='{acc1['id']}')"].scope == "story"
    assert by_addr[f"memory_read(node_id='{acc2['id']}')"].status == "SUPERSEDED"
    assert by_addr[f"memory_read(node_id='{acc2['id']}')"].scope == "story"

    # Correction text indexed in acting text of the note.
    res_fix = memory_index.search_memory(tmp_path, "quantum_fix_token", scope="room:42")
    assert [h.canonical_address for h in res_fix.hits] == [f"memory_read(node_id='{note['id']}')"]

    # Default search excludes REJECTED, SUPERSEDED, RELEASED but keeps ACTIVE and FOLDED.
    res_default = memory_index.search_memory(tmp_path, "quantum", limit=50)
    default_statuses = {h.status for h in res_default.hits}
    assert default_statuses.isdisjoint({"REJECTED", "SUPERSEDED", "RELEASED", "ARCHIVED"})
    assert f"memory_read(node_id='{acc1['id']}')" in {h.canonical_address for h in res_default.hits}
    assert f"memory_read(node_id='{p1['id']}')" in {h.canonical_address for h in res_default.hits}

    # Incremental catch-up: withdrawing acc1 restores `part` from SUPERSEDED -> ACTIVE and marks acc1 SUPERSEDED.
    assert store.select_account(acc1["id"], replaces=[], author=mind, reason="withdraw acc1", shown=False).ok
    res_after = memory_index.search_memory(tmp_path, "quantum", include_archived=True, limit=50)
    assert res_after.rebuilt is False
    by_addr_after = {h.canonical_address: h for h in res_after.hits}
    assert by_addr_after[f"memory_read(node_id='{acc1['id']}')"].status == "SUPERSEDED"
    assert by_addr_after[f"memory_read(node_id='{part['id']}')"].status == "ACTIVE"


# --- 3. Project workpads, incremental journals & tombstoned status -------------------------------


def test_workpads_incremental_journals_and_tombstone_lifecycle(tmp_path):
    _write_projects_registry(tmp_path, [
        {"id": "proj-open", "name": "Open Project", "lifecycle": "active"},
        {"id": "proj-done", "name": "Done Project", "lifecycle": "tombstoned"},
    ])
    for pid in ("proj-open", "proj-done"):
        pdir = tmp_path / "projects" / pid
        pdir.mkdir(parents=True)
        (pdir / "workpad.md").write_text(f"# {pid} Plan\nShared hyperion workpad text.\n", encoding="utf-8")
        (pdir / "journal.jsonl").write_text(
            json.dumps({"ts": "2026-11-20T10:00:00Z", "kind": "start", "text": f"{pid} hyperion start", "task_id": "t1"}) + "\n",
            encoding="utf-8",
        )

    res = memory_index.search_memory(tmp_path, "hyperion", limit=20)
    by_key = {h.doc_key: h for h in res.hits}
    assert by_key["workpad:proj-open"].status == "ACTIVE"
    assert by_key["workpad:proj-open"].canonical_address == "workpad_read(project_id='proj-open')"
    assert by_key["workpad:proj-done"].status == "CLOSED"
    assert by_key["journal:proj-open:0"].status == "ACTIVE"
    assert by_key["journal:proj-open:0"].canonical_address == "journal_read(project_id='proj-open')"
    assert by_key["journal:proj-done:0"].status == "CLOSED"

    # Append a new journal line -> incremental byte-offset catch-up without rebuild.
    jn_path = tmp_path / "projects" / "proj-open" / "journal.jsonl"
    first_size = jn_path.stat().st_size
    with jn_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-11-23T12:00:00Z", "kind": "done", "text": "hyperion_incremental_milestone", "task_id": "t2"}) + "\n")

    res2 = memory_index.search_memory(tmp_path, "hyperion_incremental_milestone")
    assert res2.rebuilt is False
    assert len(res2.hits) == 1
    assert res2.hits[0].doc_key == f"journal:proj-open:{first_size}"
    assert res2.hits[0].date_ymd == "2026-11-23"

    # Tombstone proj-open -> workpad and journal entries flip to CLOSED incrementally.
    _write_projects_registry(tmp_path, [
        {"id": "proj-open", "name": "Open Project", "lifecycle": "tombstoned"},
        {"id": "proj-done", "name": "Done Project", "lifecycle": "tombstoned"},
    ])
    res3 = memory_index.search_memory(tmp_path, "hyperion", scope="project:proj-open")
    assert res3.rebuilt is False
    assert all(h.status == "CLOSED" for h in res3.hits)


def test_journal_entries_sharing_one_address_never_crowd_out_the_brief(tmp_path):
    """More short journal rows than the fetch window match a project name by their metadata; one hit
    per address, collapsed before the fetch limit, keeps the closure brief and the workpad visible."""
    _write_projects_registry(tmp_path, [{"id": "quasar-repair", "name": "Quasar Repair", "lifecycle": "tombstoned"}])
    pdir = tmp_path / "projects" / "quasar-repair"
    pdir.mkdir(parents=True)
    (pdir / "workpad.md").write_text("# Quasar Repair\n" + "Long working notes about the repair. " * 40, encoding="utf-8")
    (pdir / "closure_brief.md").write_text(
        "---\ntitle: Quasar Repair closed\nsummary: Delivered fix.\n---\n# Quasar Repair\n" + "What happened. " * 60,
        encoding="utf-8")
    (pdir / "journal.jsonl").write_text("".join(
        json.dumps({"ts": "2026-11-20T10:00:00Z", "kind": "checkpoint", "text": f"step {n}"}) + "\n" for n in range(150)),
        encoding="utf-8")

    res = memory_index.search_memory(tmp_path, "quasar repair", limit=3)
    addresses = [h.canonical_address for h in res.hits]
    assert len(addresses) == len(set(addresses)) == 3
    assert "memory_read(node_id='closure_brief:quasar-repair')" in addresses
    assert "workpad_read(project_id='quasar-repair')" in addresses
    assert addresses.count("journal_read(project_id='quasar-repair')") == 1
    readonly = memory_index.search_readonly(tmp_path, "quasar repair", limit=3)
    assert [h.canonical_address for h in readonly.hits] == addresses


# --- 4. Closure briefs (YAML frontmatter + memory_read node_id='closure_brief:<id>') -------------


def test_closure_brief_indexing_and_memory_read_direct_access(tmp_path):
    pdir = tmp_path / "projects" / "habitat-memory"
    pdir.mkdir(parents=True)
    brief_text = (
        "---\n"
        "title: Phase 1A Observability Compaction\n"
        "summary: Packed loose call manifests into xz archives and deduplicated task_bodies.\n"
        "closed_at: '2026-11-23T10:00:00Z'\n"
        "keywords:\n"
        "  - evidence_packs\n"
        "  - store_body_dedup\n"
        "touched_paths:\n"
        "  - ouroboros/evidence_packs.py\n"
        "  - ouroboros/store_body_dedup.py\n"
        "commit_shas:\n"
        "  - 53f0695147ff\n"
        "error_signatures:\n"
        "  - SQLITE_CORRUPT:vtree_page\n"
        "supersedes:\n"
        "  - legacy-pack-plan-v0\n"
        "---\n"
        "# Closure Brief\n\nReduced observability from 893,142 files to 15,171 files.\n"
    )
    (pdir / "closure_brief.md").write_text(brief_text, encoding="utf-8")

    # Search by frontmatter commit_sha, touched_path, and error_signature (no chronicle activation needed!).
    for q in ("53f0695147ff", "ouroboros/store_body_dedup.py", "SQLITE_CORRUPT:vtree_page", "legacy-pack-plan-v0"):
        res = memory_index.search_memory(tmp_path, q)
        assert res.hit_count == 1, q
        hit = res.hits[0]
        assert hit.status == "CLOSED"
        assert hit.scope == "project:habitat-memory"
        assert hit.date_ymd == "2026-11-23"
        assert hit.canonical_address == "memory_read(node_id='closure_brief:habitat-memory')"

    # Read directly via memory_read(node_id='closure_brief:habitat-memory') without chronicle activation.
    ctx = _ctx(tmp_path)
    out = _memory_read(ctx, node_id="closure_brief:habitat-memory")
    assert "[closure_brief habitat-memory;" in out
    assert "Reduced observability from 893,142 files" in out

    # Missing brief and path traversal / revision errors are handled cleanly.
    assert "TOOL_ARG_ERROR" in _memory_read(ctx, node_id="closure_brief:no-such-project")
    assert "TOOL_ARG_ERROR" in _memory_read(ctx, node_id="closure_brief:../escape")
    assert "TOOL_ARG_ERROR" in _memory_read(ctx, node_id="closure_brief:habitat-memory", revision="rev1")


# --- 5. Lazy pruning, corruption rebuild & schema mismatch rebuild -------------------------------


def test_lazy_sync_prunes_deleted_files_and_rebuilds_on_corruption_or_schema_drift(tmp_path):
    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    ephemeral = kn / "ephemeral.md"
    ephemeral.write_text("# Ephemeral\nUnique zephyr_ephemeral_token.\n", encoding="utf-8")
    keeper = kn / "keeper.md"
    keeper.write_text("# Keeper\nUnique zephyr_keeper_token.\n", encoding="utf-8")

    assert memory_index.search_memory(tmp_path, "zephyr_ephemeral_token").hit_count == 1
    ephemeral.unlink()
    res_pruned = memory_index.search_memory(tmp_path, "zephyr_ephemeral_token")
    assert res_pruned.rebuilt is False
    assert res_pruned.hit_count == 0
    assert memory_index.search_memory(tmp_path, "zephyr_keeper_token").hit_count == 1

    # Verify journal mode is DELETE (never WAL) and no -wal file exists.
    db_path = memory_index.index_path(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        conn.close()
    assert not pathlib.Path(str(db_path) + "-wal").exists()

    # Corrupt the SQLite file with garbage bytes -> automatic atomic rebuild.
    db_path.write_bytes(b"not a valid sqlite database at all" * 64)
    res_corrupt = memory_index.search_memory(tmp_path, "zephyr_keeper_token")
    assert res_corrupt.rebuilt is True
    assert res_corrupt.hit_count == 1

    # Bump PRAGMA user_version to simulate schema drift -> automatic rebuild.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version=999")
        conn.commit()
    finally:
        conn.close()
    res_drift = memory_index.search_memory(tmp_path, "zephyr_keeper_token")
    assert res_drift.rebuilt is True
    assert res_drift.hit_count == 1


# --- 6. Composite ranking: lifecycle weights + recency boost -------------------------------------


def test_composite_ranking_prefers_active_and_closure_over_folded_and_archived_and_boosts_recency(tmp_path):
    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    # Identical text across notes with different dates and lifecycle states.
    (kn / "active-new.md").write_text("---\ntitle: New Active\nupdated: 2026-11-23\n---\norion_rank_needle\n", encoding="utf-8")
    (kn / "active-old.md").write_text("---\ntitle: Old Active\nupdated: 2025-01-01\n---\norion_rank_needle\n", encoding="utf-8")
    (kn / "archived-new.md").write_text(
        "---\ntitle: New Archived\nupdated: 2026-11-23\narchive:\n  at: '2026-11-23T00:00:00Z'\n  reason: old\n---\n"
        "orion_rank_needle\n",
        encoding="utf-8",
    )

    pdir = tmp_path / "projects" / "closed-p"
    pdir.mkdir(parents=True)
    (pdir / "closure_brief.md").write_text(
        "---\ntitle: Closure Brief\nclosed_at: '2026-11-22T00:00:00Z'\n---\norion_rank_needle\n",
        encoding="utf-8",
    )

    store = ChronicleStore(tmp_path)
    mind = _mind()
    p1 = store.publish_page(
        room_id="1", text="orion_rank_needle",
        covers={"room_id": "1", "rows": ["r1"], "stream_span": [1, 1]}, author=mind,
    ).record
    # Pin page date to today so even fresh folded loses to old active/closure.
    store.publish_part(
        room_id="1", text="unrelated parent chapter summary",
        member_ids=[p1["id"]], author=mind, expected_sequence=store.room_head("1"),
    )

    today = datetime.date(2026, 11, 23)
    res = memory_index.search_memory(tmp_path, "orion_rank_needle", include_archived=True, limit=10, today=today)
    order = [h.doc_key for h in res.hits]

    assert order.index("knowledge:global:active-new") < order.index("knowledge:global:active-old")
    assert order.index("knowledge:global:active-old") < order.index(f"chronicle:{p1['id']}")
    assert order.index("closure_brief:closed-p") < order.index(f"chronicle:{p1['id']}")
    assert order.index(f"chronicle:{p1['id']}") < order.index("knowledge:global:archived-new")


# --- 7. FTS5 special characters & badge formatting -----------------------------------------------


@pytest.mark.parametrize("raw_query", [
    "ouroboros/tools/chronicle.py",
    "53f0695147ff",
    "WI-2.1",
    "closure_brief:alpha",
    'unmatched "quote here',
    "NOT OR AND NEAR",
    "foo:bar (baz) * ^ - +",
    "--- ::: ???",
    "'single' \"double\" `backtick`",
])
def test_fts5_special_characters_never_raise_syntax_error(tmp_path, raw_query):
    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    (kn / "special.md").write_text(
        "---\ntitle: Special Chars\nupdated: 2026-11-23\n---\n"
        "Touched ouroboros/tools/chronicle.py at 53f0695147ff for WI-2.1 and closure_brief:alpha.\n",
        encoding="utf-8",
    )
    res = memory_index.search_memory(tmp_path, raw_query)
    formatted = memory_index.format_search_result(res)
    assert formatted.startswith("memory_search:")
    for hit in res.hits:
        badge = f"[{hit.status} | {hit.date_ymd} | {hit.scope} | {hit.canonical_address}]"
        assert badge in formatted


# --- 8. memory_read tool wiring, mutual exclusivity, schema count & logs/tools.jsonl telemetry ---


def test_memory_read_query_tool_surface_validation_and_telemetry(tmp_path):
    # Zero new Turn-1 tool schemas: chronicle_tools() keeps exactly its 3 existing tools.
    tools = chronicle_tools()
    assert [t.name for t in tools] == ["chronicle_write", "memory_read", "memory_mark"]
    props = next(t.schema["parameters"]["properties"] for t in tools if t.name == "memory_read")
    assert {"query", "scope", "include_archived", "limit"} <= set(props)

    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    (kn / "telemetry.md").write_text(
        "---\ntitle: Telemetry Note\nupdated: 2026-11-23\n---\n"
        "Verifying structured telemetry_needle in logs/tools.jsonl.\n",
        encoding="utf-8",
    )

    # Works without chronicle activation!
    assert not (tmp_path / "memory" / "chronicle" / "records.jsonl").exists()
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(_ctx(tmp_path))

    res = registry.execute_result("memory_read", {"query": "telemetry_needle", "scope": "global", "limit": 5})
    assert res.status == "ok"
    assert "[ACTIVE | 2026-11-23 | global | knowledge_read(topic='telemetry', scope='global')]" in res.text
    assert res.meta["memory_search_query"] == "telemetry_needle"
    assert res.meta["memory_search_scope"] == "global"
    assert res.meta["memory_search_include_archived"] is False
    assert res.meta["memory_search_limit"] == 5
    assert res.meta["memory_search_hits"] == 1
    assert isinstance(res.meta["memory_search_elapsed_ms"], int) and res.meta["memory_search_elapsed_ms"] >= 0
    assert res.meta["memory_search_rebuilt"] is True

    # Second search does not rebuild.
    res2 = registry.execute_result("memory_read", {"query": "telemetry_needle"})
    assert res2.meta["memory_search_rebuilt"] is False

    # Mutual exclusivity & argument validation.
    for bad_args in (
        {"query": "x", "node_id": "abc"},
        {"query": "x", "rows": True},
        {"query": "x", "room_id": "1"},
        {"query": "x", "after_seq": 1},
        {"query": "x", "revision": "r1"},
        {"query": "x", "source_ref": {"path": "a"}},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 51},
        {"query": "x", "scope": "invalid_scope"},
        {"scope": "global"},
        {"include_archived": True},
        {"limit": 5},
    ):
        err = registry.execute_result("memory_read", bad_args)
        assert err.status == "error" and err.code == "TOOL_ARG_ERROR", (bad_args, err)


def test_memory_search_telemetry_flows_into_logs_tools_jsonl(tmp_path):
    from ouroboros.loop_tool_execution import _execute_single_tool

    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    (kn / "pipe.md").write_text(
        "---\ntitle: Pipeline Note\nupdated: 2026-11-23\n---\nChecking logs_tools_pipeline_needle.\n",
        encoding="utf-8",
    )
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(_ctx(tmp_path))

    tc = {
        "id": "call_mem_search_1",
        "function": {
            "name": "memory_read",
            "arguments": json.dumps({"query": "logs_tools_pipeline_needle", "scope": "global", "limit": 7}),
        },
    }
    out = _execute_single_tool(
        tools=registry,
        tc=tc,
        drive_logs=drive_logs,
        task_id="root0001",
    )
    assert not out["is_error"]
    tools_log = drive_logs / "tools.jsonl"
    assert tools_log.is_file()
    rows = [json.loads(line) for line in tools_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    settled = [r for r in rows if r.get("type") == "tool_call" and r.get("tool") == "memory_read"]
    assert settled, rows
    meta = settled[-1].get("tool_result_meta") or {}
    assert meta["memory_search_query"] == "logs_tools_pipeline_needle"
    assert meta["memory_search_scope"] == "global"
    assert meta["memory_search_include_archived"] is False
    assert meta["memory_search_limit"] == 7
    assert meta["memory_search_hits"] == 1
    assert isinstance(meta["memory_search_elapsed_ms"], int)
    assert meta["memory_search_rebuilt"] is True


# --- 9. Maintenance job `memory_index` -----------------------------------------------------------


def test_maintenance_memory_index_job_syncs_and_rests(tmp_path):
    from ouroboros import store

    assert "memory_index" in maintenance.JOBS
    with store.open_store(tmp_path, write=True):
        pass  # initialize current.sqlite

    # No sources yet -> not due.
    assert "memory_index" not in maintenance.due(tmp_path)

    # Add a knowledge note -> becomes due -> runs -> rests on unchanged inputs.
    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    (kn / "maint.md").write_text("# Maint\nmaintenance_job_needle\n", encoding="utf-8")

    assert "memory_index" in maintenance.due(tmp_path)
    outcome = maintenance.run_job(tmp_path, "memory_index")
    assert outcome is not None and outcome["state"] == "done" and outcome["items"] >= 1
    assert memory_index.index_path(tmp_path).is_file()

    # Second run rests cleanly.
    assert "memory_index" not in maintenance.due(tmp_path)
    assert maintenance.run_job(tmp_path, "memory_index") is None

    # Deleting the index makes the job due again and rebuilds it.
    memory_index.index_path(tmp_path).unlink()
    assert "memory_index" in maintenance.due(tmp_path)
    outcome2 = maintenance.run_job(tmp_path, "memory_index")
    assert outcome2 is not None and outcome2["state"] == "done"
    assert memory_index.search_memory(tmp_path, "maintenance_job_needle").hit_count == 1


# --- 10. Review fixes: candidate exclusion, zero-write no-op sync, subagent/presence isolation ---


def test_helper_candidate_draft_is_excluded_by_default_and_tagged_candidate(tmp_path):
    store = ChronicleStore(tmp_path)
    draft = store.publish_page(
        room_id="1", text="Unadopted helper candidate_cobalt_token draft.",
        covers={"room_id": "1", "rows": ["r1"], "stream_span": [1, 1]}, author=_helper(),
        metadata={"candidate": True},
    ).record

    res_default = memory_index.search_memory(tmp_path, "candidate_cobalt_token")
    assert res_default.hit_count == 0

    res_all = memory_index.search_memory(tmp_path, "candidate_cobalt_token", include_archived=True)
    assert res_all.hit_count == 1
    assert res_all.hits[0].status == "CANDIDATE"
    assert res_all.hits[0].canonical_address == f"memory_read(node_id='{draft['id']}')"


def test_clean_noop_sync_performs_no_file_write(tmp_path):
    kn = tmp_path / "memory" / "knowledge"
    kn.mkdir(parents=True)
    (kn / "stable.md").write_text("# Stable\nnoop_sync_needle\n", encoding="utf-8")

    memory_index.search_memory(tmp_path, "noop_sync_needle")
    db_path = memory_index.index_path(tmp_path)
    mtime_before = db_path.stat().st_mtime_ns
    time.sleep(0.02)

    rep = memory_index.sync_index(tmp_path)
    assert rep == memory_index.SyncReport(synced=0, pruned=0, rebuilt=False)
    assert db_path.stat().st_mtime_ns == mtime_before


def test_non_root_actors_cannot_peek_foreign_project_stores_via_search_or_closure_brief(tmp_path):
    _write_projects_registry(tmp_path, [
        {"id": "proj-own", "name": "Own", "lifecycle": "active"},
        {"id": "proj-secret", "name": "Secret", "lifecycle": "tombstoned"},
    ])
    for pid in ("proj-own", "proj-secret"):
        pdir = tmp_path / "projects" / pid
        (pdir / "knowledge").mkdir(parents=True)
        (pdir / "workpad.md").write_text(f"# {pid}\nisolation_shared_needle {pid}_workpad_secret\n", encoding="utf-8")
        (pdir / "journal.jsonl").write_text(
            json.dumps({"ts": "2026-11-23T10:00:00Z", "kind": "note", "text": f"isolation_shared_needle {pid}_journal_secret"}) + "\n",
            encoding="utf-8",
        )
        (pdir / "closure_brief.md").write_text(
            f"---\ntitle: {pid} Brief\n---\nisolation_shared_needle {pid}_brief_secret\n", encoding="utf-8",
        )
        (pdir / "knowledge" / "shared-note.md").write_text(f"# Note\nisolation_shared_needle {pid}_kn\n", encoding="utf-8")

    # Root actor sees both projects' workpads, journals, closure briefs and knowledge.
    root_out = _memory_read(_ctx(tmp_path, delegation_role="root"), query="isolation_shared_needle", limit=20)
    assert "proj-secret_workpad_secret" in root_out
    assert "proj-secret_brief_secret" in root_out or "closure_brief:proj-secret" in root_out
    assert "Reduced" not in root_out  # sanity
    assert "[closure_brief proj-secret;" in _memory_read(_ctx(tmp_path, delegation_role="root"), node_id="closure_brief:proj-secret")

    # Delegated child placed in proj-own sees its own project and knowledge, never proj-secret's workpad/journal/brief.
    child_ctx = _ctx(tmp_path, task_id="kid1", delegation_role="subagent", project_id="proj-own")
    child_out = _memory_read(child_ctx, query="isolation_shared_needle", limit=20)
    assert "workpad_read(project_id='proj-own')" in child_out
    assert "workpad_read(project_id='proj-secret')" not in child_out
    assert "journal_read(project_id='proj-secret')" not in child_out
    assert "closure_brief:proj-secret" not in child_out
    assert "knowledge_read(topic='shared-note', scope='project:proj-secret')" in child_out

    # Foreign closure_brief direct read is refused with TOOL_FORBIDDEN.
    assert "TOOL_FORBIDDEN" in _memory_read(child_ctx, node_id="closure_brief:proj-secret")
    assert "[closure_brief proj-own;" in _memory_read(child_ctx, node_id="closure_brief:proj-own")

    # Unscoped Presence actor sees no project-private workpad/journal/closure_brief hits and cannot read closure briefs.
    presence_ctx = _ctx(tmp_path, task_id="pres1", delegation_role="root", presence=True)
    pres_out = _memory_read(presence_ctx, query="isolation_shared_needle", limit=20)
    assert "workpad_read" not in pres_out
    assert "journal_read" not in pres_out
    assert "closure_brief:" not in pres_out
    assert "TOOL_FORBIDDEN" in _memory_read(presence_ctx, node_id="closure_brief:proj-own")
