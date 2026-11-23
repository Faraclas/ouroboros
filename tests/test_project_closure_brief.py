"""Tests for WI-2.2: structured project closure briefs, Project Close wiring, and backfill."""
from __future__ import annotations

import json
import pathlib
import types
from unittest import mock

import pytest
import yaml

from ouroboros import (
    config as cfg,
    memory_index,
    project_closure,
    project_facts,
    project_management,
    projects_registry,
)
from ouroboros.chronicle_store import ChronicleStore
from ouroboros.tools.chronicle import _memory_read
from ouroboros.tools.registry import ToolContext


def _ctx(drive_root: pathlib.Path, *, project_id: str = "", delegation_role: str = "root") -> ToolContext:
    ctx = ToolContext(
        repo_dir=drive_root / "repo",
        drive_root=drive_root,
        task_id="task-test",
        project_id=project_id,
    )
    ctx.task_metadata = {"delegation_role": delegation_role, "root_task_id": "task-test"}
    return ctx


def _parse_frontmatter(path: pathlib.Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    _, fm_text, body = raw.split("---\n", 2)
    parsed = yaml.safe_load(fm_text)
    assert isinstance(parsed, dict)
    return parsed, body


def _make_project(drive_root: pathlib.Path, name: str, *, working_dir: str = "", description: str = "", notes: str = "") -> dict:
    pid = project_facts.project_id_from_display_name(name)
    row = projects_registry.create_project(drive_root, pid, name=name, working_dir=working_dir)
    if description or notes:
        project_management.update_project_content(
            drive_root,
            row["id"],
            expected_content={"description": "", "notes": ""},
            description=description,
            notes=notes,
        )
        row = projects_registry.get_reserved_project(drive_root, row["id"], strict=True)
    return row


def _write_journal(drive_root: pathlib.Path, pid: str, entries: list[dict]) -> None:
    path = drive_root / "projects" / pid / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _publish_chronicle_page(drive_root: pathlib.Path, chat_id: int, text: str, *, task_id: str = "task-chr1") -> None:
    store = ChronicleStore(drive_root)
    res = store.publish_page(
        room_id=str(chat_id),
        text=text,
        covers={
            "room_id": str(chat_id),
            "rows": [f"sha-{chat_id}-1"],
            "stream_span": [0, 0],
            "ts_span": {"start": "2026-11-22T10:00:00+00:00", "end": "2026-11-22T10:05:00+00:00", "incomplete": False},
        },
        author={"kind": "mind", "focus": {"role": "root", "task_id": task_id}, "task_id": task_id},
    )
    assert res.ok, res


# --- 1. Structured brief generation & all 7 required YAML fields ---------------------------------


def test_build_and_render_closure_brief_extracts_all_required_fields(tmp_path):
    row = _make_project(
        tmp_path,
        "Storage Compaction",
        working_dir="/workspaces/projects/Storage-Compaction",
        description="Pack loose observability manifests and deduplicate task bodies.",
        notes="Supersedes legacy-pack-plan-v0 after Phase 1A audit.",
    )
    pid, chat_id = row["id"], row["chat_id"]
    from ouroboros import project_footprint

    project_footprint.record_target(
        tmp_path, pid, "/workspaces/projects/Storage-Compaction-Footprint",
        task_id="fp01", origin="pre_existing", inspect=False,
    )
    pdir = tmp_path / "projects" / pid
    (pdir / "knowledge").mkdir(parents=True, exist_ok=True)

    (pdir / "workpad.md").write_text(
        "# Phase 1A Storage Compaction\n\n"
        "Modified `ouroboros/evidence_packs.py` and `ouroboros/store_body_dedup.py`.\n"
        "Adopted commit 53f0695147ff and pushed 4b9db8bd436c to origin/ouroboros.\n"
        "Ignored pure digits commit 20261123 and English word commit defaced.\n"
        "Hit `SQLITE_CORRUPT:vtree_page` and `TimeoutError` during initial sweep.\n",
        encoding="utf-8",
    )
    _write_journal(
        tmp_path,
        pid,
        [
            {
                "ts": "2026-11-22T09:00:00+00:00",
                "kind": "blocked",
                "text": "PREFLIGHT_LOCK_BUSY blocked lock acquisition on tests/test_evidence_packs.py",
                "task_id": "a1b2c3d4",
            },
            {
                "ts": "2026-11-23T11:00:00+00:00",
                "kind": "done",
                "text": "Phase 1A complete: verified packs on commit 9a8b7c6d5e4f and closed TOOL_EXEC_TIMEOUT.",
                "task_id": "e5f60718",
            },
        ],
    )
    (pdir / "knowledge" / "index-full.md").write_text(
        "# Knowledge Base Index\nShould never be parsed as a topic: commit deadbeef99.\n",
        encoding="utf-8",
    )
    (pdir / "knowledge" / "architecture.md").write_text(
        "---\n"
        "title: Compaction Design\n"
        "summary: Pack format v2 with call manifests.\n"
        "tags: [compaction, sqlite]\n"
        "supersedes: [pack-format-v1-draft]\n"
        "commit_shas: [1234abcd5678]\n"
        "---\n"
        "Uses `docs/PERSISTENCE.md` and `/home/agent/ouroboros/ouroboros/memory_index.py`.\n",
        encoding="utf-8",
    )
    _publish_chronicle_page(
        tmp_path,
        chat_id,
        "Chronicle note: verified scripts/check_domains.py after DatabaseError recovery.",
        task_id="f1e2d3c4",
    )

    data = project_closure.build_closure_brief_data(tmp_path, pid)
    rendered = project_closure.render_closure_brief(data)

    # Round-trip frontmatter via YAML parser
    assert rendered.startswith("---\n")
    _, fm_yaml, body = rendered.split("---\n", 2)
    fm = yaml.safe_load(fm_yaml)

    assert fm["schema_version"] == 1
    assert fm["project_id"] == pid
    assert fm["chat_id"] == chat_id
    assert fm["authored_by"] == "project_close"
    assert fm["closed_at"]

    # All 7 required fields present and populated from actual records
    assert "Storage Compaction" in fm["title"]
    assert "Pack loose observability manifests" in fm["summary"]
    assert "Phase 1A complete" in fm["summary"]

    assert pid in fm["keywords"]
    assert "architecture" in fm["keywords"]
    assert "compaction" in fm["keywords"]
    assert "done" in fm["keywords"]
    assert "index-full" not in fm["keywords"]

    assert "/workspaces/projects/Storage-Compaction" in fm["touched_paths"]
    assert "/workspaces/projects/Storage-Compaction-Footprint" in fm["touched_paths"]
    assert "ouroboros/evidence_packs.py" in fm["touched_paths"]
    assert "ouroboros/store_body_dedup.py" in fm["touched_paths"]
    assert "tests/test_evidence_packs.py" in fm["touched_paths"]
    assert "docs/PERSISTENCE.md" in fm["touched_paths"]
    assert "scripts/check_domains.py" in fm["touched_paths"]

    assert "53f0695147ff" in fm["commit_shas"]
    assert "4b9db8bd436c" in fm["commit_shas"]
    assert "9a8b7c6d5e4f" in fm["commit_shas"]
    assert "1234abcd5678" in fm["commit_shas"]
    assert "20261123" not in fm["commit_shas"]
    assert "defaced" not in fm["commit_shas"]
    assert "deadbeef99" not in fm["commit_shas"]  # from ignored index-full.md
    assert "a1b2c3d4" not in fm["commit_shas"]  # journal task_id excluded
    assert "e5f60718" not in fm["commit_shas"]

    assert "SQLITE_CORRUPT:vtree_page" in fm["error_signatures"]
    assert "PREFLIGHT_LOCK_BUSY" in fm["error_signatures"]
    assert "TOOL_EXEC_TIMEOUT" in fm["error_signatures"]
    assert "TimeoutError" in fm["error_signatures"]
    assert "DatabaseError" in fm["error_signatures"]
    assert any(sig.startswith("blocked:") for sig in fm["error_signatures"])

    assert "legacy-pack-plan-v0" in fm["supersedes"]
    assert "pack-format-v1-draft" in fm["supersedes"]
    assert "# " in body


# --- 2. Project Close wiring + FTS indexing + defensive failure isolation ------------------------


def test_complete_project_deletion_writes_and_indexes_closure_brief(tmp_path):
    row = _make_project(
        tmp_path,
        "Telemetry Cleanup",
        description="Removed stale telemetry emitters.",
    )
    pid = row["id"]
    pdir = tmp_path / "projects" / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "workpad.md").write_text(
        "# Telemetry Cleanup\nFixed `ouroboros/telemetry_unique_widget.py` in commit abcd1234ef.\n",
        encoding="utf-8",
    )

    assert projects_registry.begin_project_deletion(tmp_path, pid) is not None
    done = projects_registry.complete_project_deletion(tmp_path, pid)
    assert done is not None and done["lifecycle"] == projects_registry.PROJECT_TOMBSTONED

    brief_path = tmp_path / "projects" / pid / "closure_brief.md"
    assert brief_path.is_file()
    fm, _ = _parse_frontmatter(brief_path)
    assert fm["project_id"] == pid
    assert fm["authored_by"] == "project_close"
    assert "ouroboros/telemetry_unique_widget.py" in fm["touched_paths"]
    assert "abcd1234ef" in fm["commit_shas"]

    # Indexed into state/memory_index.sqlite immediately on close
    assert memory_index.index_path(tmp_path).is_file()
    out = _memory_read(_ctx(tmp_path), query="abcd1234ef")
    assert f"memory_read(node_id='closure_brief:{pid}')" in out
    assert "[CLOSED |" in out

    direct = _memory_read(_ctx(tmp_path), node_id=f"closure_brief:{pid}")
    assert f"[closure_brief {pid};" in direct
    assert "telemetry_unique_widget.py" in direct


def test_delete_project_also_generates_closure_brief_and_failure_never_blocks_tombstone(tmp_path):
    row = _make_project(tmp_path, "Compat Close")
    pid = row["id"]
    assert projects_registry.begin_project_deletion(tmp_path, pid) is not None
    assert projects_registry.delete_project(tmp_path, pid) is True
    assert (tmp_path / "projects" / pid / "closure_brief.md").is_file()

    # Defensive isolation: failure inside write_closure_brief must not prevent tombstoning
    row2 = _make_project(tmp_path, "Fault Tolerant Close")
    pid2 = row2["id"]
    assert projects_registry.begin_project_deletion(tmp_path, pid2) is not None
    with mock.patch("ouroboros.project_closure.write_closure_brief", side_effect=OSError("disk full")):
        done = projects_registry.complete_project_deletion(tmp_path, pid2)
    assert done is not None and done["lifecycle"] == projects_registry.PROJECT_TOMBSTONED
    persisted = projects_registry.get_reserved_project(tmp_path, pid2, strict=True)
    assert persisted["lifecycle"] == projects_registry.PROJECT_TOMBSTONED


# --- 3. Reopen + re-close refreshes auto brief, preserves manual brief when overwrite=False ------


def test_reopen_and_reclose_refreshes_auto_brief_but_preserves_manual_brief(tmp_path):
    row = _make_project(tmp_path, "Iterative Project", description="Initial pass.")
    pid = row["id"]
    pdir = tmp_path / "projects" / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "workpad.md").write_text("# Pass One\nFirst commit 1111aaaa.\n", encoding="utf-8")

    # First close -> auto-generated brief
    projects_registry.begin_project_deletion(tmp_path, pid)
    projects_registry.complete_project_deletion(tmp_path, pid)
    brief_path = pdir / "closure_brief.md"
    fm1, _ = _parse_frontmatter(brief_path)
    assert fm1["authored_by"] == "project_close"
    assert "1111aaaa" in fm1["commit_shas"]

    # Reopen, add new milestone, re-close -> auto-generated brief refreshes
    project_management.reopen_project(tmp_path, pid)
    _write_journal(
        tmp_path,
        pid,
        [{"ts": "2026-11-23T12:00:00+00:00", "kind": "done", "text": "Second pass landed commit 2222bbbb."}],
    )
    projects_registry.begin_project_deletion(tmp_path, pid)
    projects_registry.complete_project_deletion(tmp_path, pid)
    fm2, _ = _parse_frontmatter(brief_path)
    assert fm2["authored_by"] == "project_close"
    assert "2222bbbb" in fm2["commit_shas"]

    # Reopen, replace with a manually authored brief, re-close -> manual brief preserved!
    project_management.reopen_project(tmp_path, pid)
    manual_text = (
        "---\n"
        "schema_version: 1\n"
        f"project_id: {pid}\n"
        "authored_by: mind\n"
        "title: Handcrafted Final Brief\n"
        "summary: Curated by the mind with bespoke rationale.\n"
        "keywords: [handcrafted_sentinel_token]\n"
        "touched_paths: []\n"
        "commit_shas: []\n"
        "error_signatures: []\n"
        "supersedes: []\n"
        "---\n\n"
        "# Handcrafted Final Brief\n\nDo not overwrite me.\n"
    )
    brief_path.write_text(manual_text, encoding="utf-8")
    projects_registry.begin_project_deletion(tmp_path, pid)
    projects_registry.complete_project_deletion(tmp_path, pid)

    assert brief_path.read_text(encoding="utf-8") == manual_text
    # Explicit write_closure_brief(overwrite=False) still preserves manual brief
    project_management.write_closure_brief(tmp_path, pid, overwrite=False, sync_fts=True)
    assert brief_path.read_text(encoding="utf-8") == manual_text
    assert "handcrafted_sentinel_token" in _memory_read(_ctx(tmp_path), query="handcrafted_sentinel_token")

    # Explicit overwrite=True replaces even manual brief
    project_management.write_closure_brief(tmp_path, pid, overwrite=True, sync_fts=True)
    fm3, _ = _parse_frontmatter(brief_path)
    assert fm3["authored_by"] == "project_close"
    assert "2222bbbb" in fm3["commit_shas"]


# --- 4. Backfill for multiple tombstoned projects including empty workpad/journal ----------------


def test_backfill_closed_project_briefs_handles_empty_rooms_and_fts_search(tmp_path):
    # Active project must NOT get a closure brief during backfill
    active = _make_project(tmp_path, "Still Active Room")

    # Tombstoned #1: empty workpad/journal, registry metadata only (like context-problem)
    p_empty = _make_project(tmp_path, "Context Problem")
    # Tombstoned #2: empty workpad/journal, chronicle records only
    p_chr = _make_project(tmp_path, "Chronicle Only Room")
    _publish_chronicle_page(
        tmp_path,
        p_chr["chat_id"],
        "Resolved rare_chronicle_only_marker in ouroboros/context_fit.py on commit feed1234.",
    )
    # Tombstoned #3: rich workpad/journal + pre-existing brief (should be skipped on overwrite=False)
    p_existing = _make_project(tmp_path, "Already Briefed Room")

    # Tombstone all three directly via registry save to simulate pre-WI-2.2 closed projects
    for proj in (p_empty, p_chr, p_existing):
        projects_registry.begin_project_deletion(tmp_path, proj["id"])
        with mock.patch("ouroboros.project_closure.write_closure_brief"):
            projects_registry.complete_project_deletion(tmp_path, proj["id"])

    existing_brief = tmp_path / "projects" / p_existing["id"] / "closure_brief.md"
    existing_brief.parent.mkdir(parents=True, exist_ok=True)
    existing_brief.write_text(
        "---\n"
        "title: Preexisting Brief\n"
        "summary: Already written with preexisting_brief_marker.\n"
        "authored_by: human\n"
        "---\n\nPreexisting body.\n",
        encoding="utf-8",
    )

    result = project_management.backfill_closed_project_briefs(tmp_path, overwrite=False, sync_fts=True)
    assert result["total_closed"] == 3
    assert set(result["written"]) == {p_empty["id"], p_chr["id"]}
    assert result["skipped"] == [p_existing["id"]]
    assert result["indexed"] is True

    # Active room untouched
    assert not (tmp_path / "projects" / active["id"] / "closure_brief.md").exists()

    # Empty room brief has valid structured frontmatter and fallback summary
    empty_fm, _ = _parse_frontmatter(tmp_path / "projects" / p_empty["id"] / "closure_brief.md")
    assert empty_fm["project_id"] == p_empty["id"]
    assert empty_fm["summary"]
    assert empty_fm["supersedes"] == []

    # Chronicle-only room extracted chronicle text, path, and commit SHA
    chr_fm, _ = _parse_frontmatter(tmp_path / "projects" / p_chr["id"] / "closure_brief.md")
    assert "rare_chronicle_only_marker" in chr_fm["summary"]
    assert "ouroboros/context_fit.py" in chr_fm["touched_paths"]
    assert "feed1234" in chr_fm["commit_shas"]

    # Every tombstoned project's closure brief is searchable via memory_read(query=...)
    ctx = _ctx(tmp_path)
    out_empty = _memory_read(ctx, query="Context Problem")
    assert f"memory_read(node_id='closure_brief:{p_empty['id']}')" in out_empty

    out_chr = _memory_read(ctx, query="rare_chronicle_only_marker")
    assert f"memory_read(node_id='closure_brief:{p_chr['id']}')" in out_chr

    out_existing = _memory_read(ctx, query="preexisting_brief_marker")
    assert f"memory_read(node_id='closure_brief:{p_existing['id']}')" in out_existing

    # Second run with overwrite=False is a clean no-op
    result2 = project_management.backfill_closed_project_briefs(tmp_path, overwrite=False, sync_fts=True)
    assert result2["total_closed"] == 3
    assert result2["written"] == []
    assert set(result2["skipped"]) == {p_empty["id"], p_chr["id"], p_existing["id"]}


# --- 5. project_management exports & project_facts reader hint -----------------------------------


def test_project_management_exports_and_project_facts_closure_brief_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")

    # project_management exposes all three helpers cleanly
    assert project_management.generate_closure_brief is project_closure.generate_closure_brief
    assert project_management.write_closure_brief is project_closure.write_closure_brief
    assert project_management.backfill_closed_project_briefs is project_closure.backfill_closed_project_briefs

    assert project_facts.project_closure_brief_path("habitat-memory") == (
        tmp_path / "data" / "projects" / "habitat-memory" / "closure_brief.md"
    )

    msg = project_facts.project_store_access_block("projects/habitat-memory/closure_brief.md")
    assert msg is not None and msg.startswith("⚠️ ACCESS_DENIED:")
    assert "memory_read(node_id='closure_brief:habitat-memory') reads it" in msg
    assert "no tool writes it." in msg
    assert "knowledge_read" not in msg

    # Unactivated chronicle never raises during generate_closure_brief
    row = _make_project(tmp_path, "No Chronicle Room")
    rendered = project_management.generate_closure_brief(tmp_path, row["id"])
    assert f"project_id: {row['id']}" in rendered
    assert not (tmp_path / "memory" / "chronicle" / "records.jsonl").exists()
