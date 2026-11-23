"""Tests for WI-1A.4: stale_backups, settled_artifact_bulk, bounded pruned_manifest.json, pruned_by_retention readers, and test-evidence root guard."""
from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone

from ouroboros import artifact_retention, artifacts, outcomes, store
from ouroboros.gateway import task_archive
from ouroboros.storage_free import FreeSpace, GROUP_SETTLED_ARTIFACT_BULK, GROUP_STALE_BACKUPS
from ouroboros.task_results import write_task_result
from ouroboros.test_evidence import EVIDENCE_RUN_MARKER
from tests.test_storage_free import OLD, _fake_sizer, _guard, _preview, _run


def _age(path: pathlib.Path, days: float = 5.0) -> None:
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def test_stale_backups_protects_symlinks_young_and_active_backups_and_retires_aged(tmp_path, monkeypatch):
    _fake_sizer(monkeypatch)
    root = tmp_path / "data"
    # 1. Aged pre-update backup -> offered
    aged = root / "backups" / "pre-abc-20261104"
    aged.mkdir(parents=True)
    (aged / "dump.bin").write_bytes(b"b" * 8192)
    _age(aged, days=10)

    # 2. Fresh (<24h) backup -> in_use (protected)
    fresh = root / "backups" / "pre-fresh-now"
    fresh.mkdir(parents=True)
    (fresh / "dump.bin").write_bytes(b"f" * 4096)

    # 3. Symlink-targeted state/backup_dir_20261113 (mirrors live layout: state/backup_dir -> backup_dir_*) -> in_use!
    linked_target = root / "state" / "backup_dir_20261113"
    linked_target.mkdir(parents=True)
    (linked_target / "current.sqlite").write_bytes(b"s" * 4096)
    _age(linked_target, days=10)
    (root / "state" / "backup_dir").symlink_to(linked_target.name)

    # 4. Aged unlinked state/backup_dir_old -> offered
    old_state = root / "state" / "backup_dir_old"
    old_state.mkdir(parents=True)
    (old_state / "old.sqlite").write_bytes(b"o" * 4096)
    _age(old_state, days=10)

    # 5. Active skill rollback backup referenced by relative path -> in_use!
    active_skill_bak = root / "state" / "backup_dir_active"
    active_skill_bak.mkdir(parents=True)
    (active_skill_bak / "skill.py").write_text("x = 1\n", encoding="utf-8")
    _age(active_skill_bak, days=10)
    skill_state = root / "state" / "skills" / "demo-skill"
    skill_state.mkdir(parents=True)
    (skill_state / "update_backup.json").write_text(
        json.dumps({"backup_dir": "state/backup_dir_active"}),
        encoding="utf-8",
    )

    owner = FreeSpace(live=lambda _tid: False, guard=_guard, offered=(GROUP_STALE_BACKUPS,))
    view = _preview(owner, root, tmp_path)
    grp = view["groups"][0]
    assert grp["id"] == GROUP_STALE_BACKUPS
    assert grp["count"] == 2
    assert grp["in_use"] == 3  # fresh + linked_target + active_skill_bak
    assert grp["bytes"] >= 12288
    names = [item["name"] for item in grp["items"]]
    assert any("backups/pre-abc-20261104" in n and "UTC)" in n for n in names)
    assert any("state/backup_dir_old" in n and "UTC)" in n for n in names)
    assert not any("backup_dir_20261113" in n or "backup_dir_active" in n or "pre-fresh-now" in n for n in names)

    done = _run(owner, root, tmp_path, groups=(GROUP_STALE_BACKUPS,))
    assert done["run"]["removed"] + done["run"]["deleting"] == 2
    assert not aged.exists() and not old_state.exists()
    assert fresh.is_dir() and linked_target.is_dir() and active_skill_bak.is_dir()
    assert (root / "state" / "backup_dir").is_symlink() and (root / "state" / "backup_dir").resolve() == linked_target.resolve()


def _settle_row(root: pathlib.Path, task_id: str, ts_iso: str) -> None:
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "UPDATE tasks SET owes_synthesis=0, owes_terminal_projection=0, "
            "review_collection_pending=0, plan_collection_pending=0, drive_retention_at=? WHERE task_id=?",
            (str(datetime.fromisoformat(ts_iso).timestamp()), task_id),
        )


def test_settled_artifact_bulk_true_allowlist_verdict_preservation_lineage_and_pruned_readers(tmp_path, monkeypatch):
    _fake_sizer(monkeypatch)
    monkeypatch.setattr(artifact_retention, "BULK_FILE_BYTES", 1024)
    root = tmp_path / "data"
    tid = "settled01"
    art_dir = artifacts.task_artifact_dir_path(root, tid, create=True)

    # Registered deliverables (user_file, process_output, process_output_directory) > 1 KiB -> MUST STAY!
    (art_dir / "verification_ledger.json").write_text('{"ok": true}\n', encoding="utf-8")
    (art_dir / "small-summary.json").write_text('{"verdict": "pass"}\n', encoding="utf-8")
    (art_dir / "user-report.bin").write_bytes(b"u" * 4096)
    (art_dir / "declared-output.bin").write_bytes(b"p" * 4096)
    (art_dir / "declared-dir.zip").write_bytes(b"d" * 4096)
    (art_dir / ".artifact_manifest.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": {
            "user-report.bin": {"kind": "user_file", "name": "user-report.bin", "path": str(art_dir / "user-report.bin")},
            "declared-output.bin": {"kind": "process_output", "name": "declared-output.bin", "path": str(art_dir / "declared-output.bin")},
            "declared-dir.zip": {"kind": "process_output_directory", "name": "declared-dir.zip", "path": str(art_dir / "declared-dir.zip")},
        }}),
        encoding="utf-8",
    )
    (art_dir / "attachments").mkdir()
    (art_dir / "attachments" / "input.pdf").write_bytes(b"p" * 4096)
    (art_dir / "delegated_runs" / "r1").mkdir(parents=True)
    (art_dir / "delegated_runs" / "r1" / "workspace.patch").write_text("diff\n", encoding="utf-8")

    # Unregistered evictable bulk classes:
    (art_dir / "unregistered-dump.bin").write_bytes(b"z" * 4096)
    (art_dir / "__pycache__").mkdir()
    (art_dir / "__pycache__" / "mod.pyc").write_bytes(b"c" * 512)
    # Test-evidence run: compact result.json & source.json MUST STAY while raw.log & pack/ are evicted!
    te_run = art_dir / "source_handles" / "test-evidence" / "run-1"
    te_run.mkdir(parents=True)
    (te_run / EVIDENCE_RUN_MARKER).write_text("ev\n", encoding="utf-8")
    (te_run / "result.json").write_text('{"passed": true}\n', encoding="utf-8")
    (te_run / "source.json").write_text('{"sha": "abc"}\n', encoding="utf-8")
    (te_run / "raw.log").write_bytes(b"l" * 4096)
    (te_run / "raw-sub").mkdir()
    (te_run / "raw-sub" / "pack.bin").write_bytes(b"k" * 4096)
    # Unregistered bulk directory at root:
    (art_dir / "scratch-snapshot").mkdir()
    (art_dir / "scratch-snapshot" / "blob.bin").write_bytes(b"q" * 4096)

    # Terminal child under a LIVE root -> MUST NOT be offered!
    live_root = "liveroot1"
    term_child = "termchild1"
    child_art = artifacts.task_artifact_dir_path(root, term_child, create=True)
    (child_art / "unregistered-child-dump.bin").write_bytes(b"x" * 4096)

    write_task_result(
        root,
        tid,
        "completed",
        ts=OLD,
        result="done",
        artifact_status="ready",
        artifacts=[
            {"kind": "user_file", "name": "user-report.bin", "path": str(art_dir / "user-report.bin"), "size": 4096, "sha256": "1" * 64, "status": "ready"},
            {"kind": "process_output", "name": "declared-output.bin", "path": str(art_dir / "declared-output.bin"), "size": 4096, "sha256": "2" * 64, "status": "ready"},
            {"kind": "task_artifact", "name": "unregistered-dump.bin", "path": str(art_dir / "unregistered-dump.bin"), "size": 4096, "sha256": "3" * 64, "status": "ready"},
            {"kind": "directory_manifest", "name": "scratch-snapshot", "path": str(art_dir / "scratch-snapshot"), "is_directory": True, "file_count": 1, "size": 4096, "sha256": "", "status": "ready"},
        ],
    )
    _settle_row(root, tid, OLD)
    write_task_result(root, live_root, "running", ts=datetime.now(timezone.utc).isoformat())
    write_task_result(root, term_child, "completed", ts=OLD, root_task_id=live_root, parent_task_id=live_root, artifact_status="ready")
    _settle_row(root, term_child, OLD)

    owner = FreeSpace(
        live=lambda t: t == live_root,
        guard=_guard,
        offered=(GROUP_SETTLED_ARTIFACT_BULK,),
    )
    view = _preview(owner, root, tmp_path)
    grp = view["groups"][0]
    assert grp["id"] == GROUP_SETTLED_ARTIFACT_BULK
    assert grp["count"] == 1
    assert grp["in_use"] == 1  # term_child held because live_root is running
    assert grp["items"][0]["id"] == f"bulk:bulk_{tid}"

    done = _run(owner, root, tmp_path, groups=(GROUP_SETTLED_ARTIFACT_BULK,))
    assert done["run"]["removed"] + done["run"]["deleting"] == 1

    # All registered deliverables & compact verdicts MUST remain intact!
    assert (art_dir / "verification_ledger.json").is_file()
    assert (art_dir / "small-summary.json").is_file()
    assert (art_dir / "user-report.bin").is_file()
    assert (art_dir / "declared-output.bin").is_file()
    assert (art_dir / "declared-dir.zip").is_file()
    assert (art_dir / "attachments" / "input.pdf").is_file()
    assert (art_dir / "delegated_runs" / "r1" / "workspace.patch").is_file()
    assert (te_run / "result.json").is_file()
    assert (te_run / "source.json").is_file()
    assert (te_run / EVIDENCE_RUN_MARKER).is_file()
    assert (child_art / "unregistered-child-dump.bin").is_file()

    # Unregistered bulk & heavy raw test-evidence files are gone!
    assert not (art_dir / "unregistered-dump.bin").exists()
    assert not (art_dir / "__pycache__").exists()
    assert not (art_dir / "scratch-snapshot").exists()
    assert not (te_run / "raw.log").exists()
    assert not (te_run / "raw-sub").exists()

    # Bounded tombstone written & excluded from collect_task_artifact_records.
    tombstone = json.loads((art_dir / artifact_retention.PRUNED_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert tombstone["status"] == artifact_retention.PRUNED_STATUS
    pruned_rels = {e["relpath"] for e in tombstone["entries"]}
    assert {"unregistered-dump.bin", "__pycache__", "scratch-snapshot", "source_handles/test-evidence/run-1/raw.log"} <= pruned_rels
    collected_names = {r["name"] for r in artifacts.collect_task_artifact_records(root, tid)}
    assert artifact_retention.PRUNED_MANIFEST_NAME not in collected_names

    # Stored rows with EXPLICIT status="ready" project to pruned_by_retention in BOTH outcomes & gateway detail!
    stored_rows = [
        {"kind": "user_file", "name": "user-report.bin", "path": str(art_dir / "user-report.bin"), "size": 4096, "sha256": "1" * 64, "status": "ready"},
        {"kind": "task_artifact", "name": "unregistered-dump.bin", "path": str(art_dir / "unregistered-dump.bin"), "size": 4096, "sha256": "3" * 64, "status": "ready"},
        {"kind": "directory_manifest", "name": "scratch-snapshot", "path": str(art_dir / "scratch-snapshot"), "is_directory": True, "file_count": 1, "size": 4096, "sha256": "", "status": "ready"},
    ]
    bundle = outcomes.artifact_bundle_from_result({"artifacts": stored_rows})
    by_name = {r["name"]: r["status"] for r in bundle["artifacts"]}
    assert by_name["user-report.bin"] == "ready"
    assert by_name["unregistered-dump.bin"] == "pruned_by_retention"
    assert by_name["scratch-snapshot"] == "pruned_by_retention"
    assert bundle["status"] == "ready"

    projected_rows = artifact_retention.project_pruned_artifacts([art_dir], stored_rows)
    assert {r["name"]: r["status"] for r in projected_rows} == by_name

    # Gateway readers return 410 pruned_by_retention for pruned file and pruned directory archive!
    file_resp = task_archive.serve_task_file(root, art_dir, "unregistered-dump.bin", "unregistered-dump.bin", None, task_id=tid)
    assert file_resp.status_code == 410
    assert json.loads(file_resp.body)["reason_code"] == "pruned_by_retention"

    arch_resp = task_archive.serve_directory_archive(root, tid, "scratch-snapshot.zip", "scratch-snapshot", other_selectors=False)
    assert arch_resp.status_code == 410
    assert json.loads(arch_resp.body)["reason_code"] == "pruned_by_retention"


def test_shell_outputs_refuses_promoting_raw_test_evidence_directory_to_artifact_root(tmp_path):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.shell_outputs import _register_process_outputs

    work = tmp_path / "work"
    ev = work / "qual-run"
    ev.mkdir(parents=True)
    (ev / EVIDENCE_RUN_MARKER).write_text("test evidence\n", encoding="utf-8")
    (ev / "raw.log").write_text("huge\n", encoding="utf-8")
    drive = tmp_path / "data"
    drive.mkdir()
    ctx = ToolContext(repo_dir=work, drive_root=drive, workspace_root=work, task_id="evguard01")

    note, failed, registered, produced = _register_process_outputs(ctx, ["qual-run"], work)
    assert failed and not registered
    assert "refused promoting raw test-evidence directory" in note
    assert artifacts.collect_task_artifact_records(drive, "evguard01") == []
