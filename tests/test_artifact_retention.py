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


def _write_fresh_queue_snapshot(root: pathlib.Path, *, running: Optional[list] = None) -> None:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    (state_dir / "queue_snapshot.json").write_text(
        json.dumps({"generation": 1, "ts": now_iso, "running": running or [], "pending": []}),
        encoding="utf-8",
    )
    (state_dir / "queue.alive").write_text(
        json.dumps({"generation": 1, "ts": now_iso, "heartbeats": {}}),
        encoding="utf-8",
    )


def test_maintenance_artifact_retention_7d_window_and_allowlist(tmp_path, monkeypatch):
    from ouroboros import artifact_retention_job, maintenance, maintenance_jobs
    from ouroboros.task_status import task_has_live_queue_ownership

    root = tmp_path / "data"
    now = time.time()
    _write_fresh_queue_snapshot(root, running=[{"id": "live01", "task": {"id": "live01", "root_task_id": "live01"}}])
    assert task_has_live_queue_ownership(root, "young01") is False
    assert task_has_live_queue_ownership(root, "live01") is True

    monkeypatch.setattr(artifact_retention, "BULK_FILE_BYTES", 1024)

    def _seed_task(tid: str, age_days: float, *, status: str = "completed", review_pending: int = 0) -> pathlib.Path:
        ts_iso = datetime.fromtimestamp(now - age_days * 86400, tz=timezone.utc).isoformat()
        art = artifacts.task_artifact_dir_path(root, tid, create=True)
        (art / "user-deliverable.bin").write_bytes(b"u" * 2048)
        (art / ".artifact_manifest.json").write_text(
            json.dumps({"schema_version": 1, "artifacts": {
                "user-deliverable.bin": {"kind": "user_file", "name": "user-deliverable.bin", "path": str(art / "user-deliverable.bin")},
            }}),
            encoding="utf-8",
        )
        (art / "bulk-dump.bin").write_bytes(b"b" * 4096)
        (art / "bulk-dir").mkdir()
        (art / "bulk-dir" / "raw.log").write_bytes(b"r" * 4096)
        write_task_result(root, tid, status, ts=ts_iso, result="ok", artifact_status="ready")
        with store.open_store(root, write=True) as txn:
            txn.conn.execute(
                "UPDATE tasks SET owes_synthesis=0, owes_terminal_projection=0, "
                "review_collection_pending=?, plan_collection_pending=0, drive_retention_at=? WHERE task_id=?",
                (review_pending, str(now - age_days * 86400), tid),
            )
        return art

    young_art = _seed_task("young01", age_days=2.0)
    old_art = _seed_task("old01", age_days=9.0)
    held_art = _seed_task("held01", age_days=12.0, review_pending=1)
    live_art = _seed_task("live01", age_days=12.0)

    assert maintenance.due(root) and "artifact_retention" in maintenance.due(root)
    report = maintenance.run(root, should_stop=lambda: False)
    assert report["jobs"]["artifact_retention"]["state"] == "done"
    assert report["jobs"]["artifact_retention"]["items"] == 1

    # old01 bulk pruned and custody_trash swept in the same maintenance pass; deliverable intact!
    assert (old_art / "user-deliverable.bin").is_file()
    assert not (old_art / "bulk-dump.bin").exists()
    assert not (old_art / "bulk-dir").exists()
    assert (old_art / artifact_retention.PRUNED_MANIFEST_NAME).is_file()
    assert not any((root / "state" / "custody_trash").glob("bulk_old01*"))

    # young01 (<7d), held01 (review_collection_pending=1), and live01 (in queue_snapshot running) untouched!
    assert (young_art / "bulk-dump.bin").is_file() and (young_art / "bulk-dir").is_dir()
    assert (held_art / "bulk-dump.bin").is_file() and (held_art / "bulk-dir").is_dir()
    assert (live_art / "bulk-dump.bin").is_file() and (live_art / "bulk-dir").is_dir()

    # Immediate second run rests cleanly.
    assert artifact_retention_job.due(root, maintenance_jobs.job(root, "artifact_retention") or {}, {}, True) is False
    assert maintenance.run_job(root, "artifact_retention") is None

    # Advance young01 past 7-day grace window -> becomes due and prunes!
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "UPDATE tasks SET drive_retention_at=? WHERE task_id='young01'",
            (str(now - 8.0 * 86400),),
        )
    row = maintenance_jobs.job(root, "artifact_retention") or {}
    row["next_due"] = now - 60
    row["young"]["young01"] = now - 60
    maintenance_jobs.record(root, "artifact_retention", **row)
    assert artifact_retention_job.due(root, maintenance_jobs.job(root, "artifact_retention") or {}, {}, True) is True
    res2 = maintenance.run_job(root, "artifact_retention")
    assert res2 == {"state": "done", "items": 1}
    assert (young_art / "user-deliverable.bin").is_file()
    assert not (young_art / "bulk-dump.bin").exists()
    assert not (young_art / "bulk-dir").exists()
    assert (held_art / "bulk-dump.bin").is_file()


def test_maintenance_closed_project_pruning_requires_verified_fts5_closure_brief(tmp_path):
    from ouroboros import maintenance, memory_index, projects_registry

    root = tmp_path / "data"
    now = time.time()
    _write_fresh_queue_snapshot(root)

    pid = "closed-alpha"
    projects_registry.create_project(root, pid, name="Closed Alpha")
    projects_registry.begin_project_deletion(root, pid)
    # Write tombstoned registry entry directly without closure_brief.md first (fail-closed check).
    with projects_registry._file_write_lock(projects_registry._registry_path(root)):
        reg = projects_registry._load(root, strict=True, project_id=pid)
        for entry in reg["projects"]:
            if entry["id"] == pid:
                entry["lifecycle"] = projects_registry.PROJECT_TOMBSTONED
                entry["tombstoned_at"] = datetime.now(timezone.utc).isoformat()
        projects_registry._save(root, reg)

    tid = "closedtask01"
    art = artifacts.task_artifact_dir_path(root, tid, create=True)
    (art / "deliverable.txt").write_text("keep me\n", encoding="utf-8")
    (art / ".artifact_manifest.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": {
            "deliverable.txt": {"kind": "user_file", "name": "deliverable.txt", "path": str(art / "deliverable.txt")},
        }}),
        encoding="utf-8",
    )
    (art / "compact-verdict.json").write_text('{"verdict": "ok"}\n', encoding="utf-8")
    # Small (<10 MiB) raw dump & directory — only closed-project pruning removes <10 MiB bulk!
    (art / "small-raw.log").write_bytes(b"s" * 512)
    (art / "raw-dir").mkdir()
    (art / "raw-dir" / "trace.bin").write_bytes(b"t" * 512)
    recent_iso = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    write_task_result(root, tid, "completed", project_id=pid, ts=recent_iso, result="ok", artifact_status="ready")
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "UPDATE tasks SET owes_synthesis=0, owes_terminal_projection=0, "
            "review_collection_pending=0, plan_collection_pending=0, drive_retention_at=? WHERE task_id=?",
            (str(now - 60), tid),
        )

    # 1. Missing closure_brief.md -> FAIL CLOSED!
    assert memory_index.is_closure_brief_indexed(root, pid) is False
    maintenance.run(root, should_stop=lambda: False)
    assert (art / "small-raw.log").is_file() and (art / "raw-dir").is_dir()

    # 2. closure_brief.md exists on disk, NOT yet in memory_index.sqlite -> FAIL CLOSED!
    brief_path = root / "projects" / pid / "closure_brief.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text("---\nschema_version: 1\nproject_id: closed-alpha\ntitle: Closed Alpha\n---\nSummary.\n", encoding="utf-8")
    assert memory_index.is_closure_brief_indexed(root, pid) is False
    maintenance.run_job(root, "artifact_retention")
    assert (art / "small-raw.log").is_file() and (art / "raw-dir").is_dir()

    # 3. Sync memory_index, then mutate closure_brief.md without re-syncing (stale index) -> FAIL CLOSED!
    memory_index.sync_index(root)
    assert memory_index.is_closure_brief_indexed(root, pid) is True
    brief_path.write_text("---\nschema_version: 1\nproject_id: closed-alpha\ntitle: Closed Alpha Updated\n---\nNew.\n", encoding="utf-8")
    os.utime(brief_path, (now + 10, now + 10))
    assert memory_index.is_closure_brief_indexed(root, pid) is False
    maintenance.run_job(root, "artifact_retention")
    assert (art / "small-raw.log").is_file() and (art / "raw-dir").is_dir()

    # 4. Run full maintenance.run(root): memory_index indexes updated brief, then artifact_retention prunes, then custody_trash deletes!
    report = maintenance.run(root, should_stop=lambda: False)
    assert memory_index.is_closure_brief_indexed(root, pid) is True
    assert report["jobs"]["artifact_retention"]["items"] == 1
    assert (art / "deliverable.txt").is_file()
    assert (art / "compact-verdict.json").is_file()
    assert not (art / "small-raw.log").exists()
    assert not (art / "raw-dir").exists()
    assert (art / artifact_retention.PRUNED_MANIFEST_NAME).is_file()
    resp = task_archive.serve_task_file(root, art, "small-raw.log", "small-raw.log", None, task_id=tid)
    assert resp.status_code == 410 and json.loads(resp.body)["reason_code"] == "pruned_by_retention"


def test_artifact_retention_unreadable_registry_rests_and_child_lineage_and_reopen_guard(tmp_path):
    from ouroboros import artifact_retention_job, maintenance, maintenance_jobs, memory_index, project_management, projects_registry, storage_removers

    root = tmp_path / "data"
    now = time.time()
    _write_fresh_queue_snapshot(root)

    # 1. Corrupt projects.json -> fail closed AND rest on second check until projects.json changes!
    pid = "closed-beta"
    projects_registry.create_project(root, pid, name="Closed Beta")
    child_tid = "childnullpid01"
    root_tid = "rootpid01"
    recent_iso = datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat()
    write_task_result(root, root_tid, "completed", project_id=pid, ts=recent_iso, result="ok", artifact_status="ready")
    write_task_result(
        root, child_tid, "completed", project_id="", root_task_id=root_tid, parent_task_id=root_tid,
        ts=recent_iso, result="ok", artifact_status="ready",
    )
    child_art = artifacts.task_artifact_dir_path(root, child_tid, create=True)
    (child_art / "child-raw.log").write_bytes(b"c" * 512)
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "UPDATE tasks SET owes_synthesis=0, owes_terminal_projection=0, "
            "review_collection_pending=0, plan_collection_pending=0, drive_retention_at=? WHERE task_id IN (?, ?)",
            (str(now - 30), root_tid, child_tid),
        )

    reg_path = root / "state" / "projects.json"
    saved_reg = reg_path.read_text(encoding="utf-8")
    reg_path.write_text("{broken json", encoding="utf-8")
    first = maintenance.run_job(root, "artifact_retention")
    assert first == {"state": "done", "items": 0}
    assert (child_art / "child-raw.log").is_file()
    assert (maintenance_jobs.job(root, "artifact_retention") or {}).get("registry_unreadable") is True
    # Rests with no repeated spawn while projects.json stays unchanged!
    assert "artifact_retention" not in maintenance.due(root)
    assert maintenance.run_job(root, "artifact_retention") is None

    # 2. Restore registry, tombstone + write & index brief -> child with project_id='' is discovered via root lineage and pruned!
    reg_path.write_text(saved_reg, encoding="utf-8")
    os.utime(reg_path, (now + 5, now + 5))
    projects_registry.begin_project_deletion(root, pid)
    projects_registry.complete_project_deletion(root, pid)
    memory_index.sync_index(root)
    assert memory_index.is_closure_brief_indexed(root, pid) is True
    res = maintenance.run_job(root, "artifact_retention")
    assert res == {"state": "done", "items": 1}
    assert not (child_art / "child-raw.log").exists()
    assert (child_art / artifact_retention.PRUNED_MANIFEST_NAME).is_file()

    # 3. Mid-pass reopen guard: _still_closed_and_verified returns False if project is reopened!
    sig = artifact_retention_job._stat_sig(root / "projects" / pid / "closure_brief.md")
    assert artifact_retention_job._still_closed_and_verified(root, pid, sig) is True
    project_management.reopen_project(root, pid)
    assert artifact_retention_job._still_closed_and_verified(root, pid, sig) is False

    # 4. storage_removers surfaces artifact_retention under task_results!
    removers = storage_removers.place_removers(root, engine=False)
    assert any(r["id"] == "artifact_retention" and r["state"] == "done" for r in removers["task_results"])

    # 5. Active-project task kept fail-closed while registry unreadable is revisited when registry is repaired!
    active_pid = "active-recover"
    projects_registry.create_project(root, active_pid, name="Active Recover")
    old_ts = datetime.fromtimestamp(now - 10 * 86400, tz=timezone.utc).isoformat()
    act_tid = "actrecover01"
    write_task_result(root, act_tid, "completed", project_id=active_pid, ts=old_ts, result="ok", artifact_status="ready")
    act_art = artifacts.task_artifact_dir_path(root, act_tid, create=True)
    (act_art / "big-dump.bin").write_bytes(b"a" * (artifact_retention.BULK_FILE_BYTES + 1024))
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "UPDATE tasks SET owes_synthesis=0, owes_terminal_projection=0, "
            "review_collection_pending=0, plan_collection_pending=0, drive_retention_at=? WHERE task_id=?",
            (str(now - 10 * 86400), act_tid),
        )
    saved_reg2 = reg_path.read_text(encoding="utf-8")
    reg_path.write_text("{broken", encoding="utf-8")
    os.utime(reg_path, (now + 20, now + 20))
    maintenance.run_job(root, "artifact_retention")
    assert (act_art / "big-dump.bin").is_file()
    assert act_tid in (maintenance_jobs.job(root, "artifact_retention") or {}).get("waiting", [])
    reg_path.write_text(saved_reg2, encoding="utf-8")
    os.utime(reg_path, (now + 30, now + 30))
    res2 = maintenance.run_job(root, "artifact_retention")
    assert res2 == {"state": "done", "items": 1}
    assert not (act_art / "big-dump.bin").exists()
    assert (act_art / artifact_retention.PRUNED_MANIFEST_NAME).is_file()


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
