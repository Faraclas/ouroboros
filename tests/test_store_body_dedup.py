"""Tests for WI-1A.3: lossless task_bodies 2x JSON deduplication, compaction, and bulk directory_manifest summarization."""
from __future__ import annotations

import json
import pathlib
import zipfile

from ouroboros import artifacts, maintenance, store, store_body_dedup, task_store
from ouroboros.gateway import task_archive
from ouroboros.headless import copy_child_task_result
from ouroboros.outcomes import artifact_bundle_from_result
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.task_status import load_effective_task_result


def _sample_result(drive: pathlib.Path, task_id: str, *, with_status: bool = True) -> dict:
    art_dir = artifacts.task_artifact_dir_path(drive, task_id, create=True)
    files = []
    for i in range(20):
        p = art_dir / f"report_{i:02d}.json"
        p.write_text(json.dumps({"i": i, "payload": "x" * 64}), encoding="utf-8")
        row = {
            "kind": "process_output",
            "name": p.name,
            "path": str(p),
            "size": p.stat().st_size,
            "sha256": f"{i:064x}",
            "errors": [],
            "source_path": f"/tmp/src/report_{i:02d}.json",
        }
        if with_status:
            row["status"] = "ready"
        files.append(row)
    trace_refs = {
        "execution_id": f"exec-{task_id}",
        "llm_call_refs": [
            {
                "llm_call_id": f"call-{i}",
                "execution_id": f"exec-{task_id}",
                "round_id": f"r-{i}",
                "round": i + 1,
                "request_ref": {"kind": "blob", "sha256": f"{i:064x}"},
                "response_ref": {"kind": "blob", "sha256": f"{i + 100:064x}"},
                "model": "test-model",
                "resolved_model": "test-model",
                "provider": "test",
                "reported_model": "test-model",
                "use_local": False,
                "usable_solve_response": True,
                "manifest_ref": f"observability/calls/{task_id}/call-{i}.json",
            }
            for i in range(15)
        ],
        "tool_call_refs": [
            {"call_id": f"tc-{i}", "manifest_ref": f"m-{i}", "redacted_projection_ref": None, "redaction": None}
            for i in range(15)
        ],
    }
    doc = {
        "task_id": task_id,
        "status": "completed",
        "ts": "2026-11-23T05:00:00+00:00",
        "result": "done",
        "artifacts": files,
        "trace_refs": trace_refs,
        "loop_outcome": {
            "status": "completed",
            "usage": {"total_rounds": 15},
            "trace_refs": json.loads(json.dumps(trace_refs)),
        },
    }
    doc["artifact_bundle"] = artifact_bundle_from_result(doc)
    return doc


def test_dedup_round_trips_exact_bytes_and_shrinks_stored_body(tmp_path):
    doc = _sample_result(tmp_path, "dedup01", with_status=True)
    raw_full = store.document_text(doc)
    encoded = store.encode_document(doc)
    assert len(encoded) < len(raw_full) * 0.65

    stored = json.loads(encoded)
    assert stored["loop_outcome"]["trace_refs"] == {"$dedup": "trace_refs"}
    assert stored["artifact_bundle"]["artifacts"] == {"$dedup": "artifacts"}
    assert stored["artifact_bundle"]["status"] == "ready"
    assert stored["loop_outcome"]["usage"] == {"total_rounds": 15}

    expanded = store_body_dedup.expand(stored)
    assert expanded == doc
    assert store.document_text(expanded) == raw_full


def test_dedup_preserves_status_overrides_when_artifact_lacks_explicit_status(tmp_path):
    doc = _sample_result(tmp_path, "dedup02", with_status=False)
    assert "status" not in doc["artifacts"][0]
    assert doc["artifact_bundle"]["artifacts"][0]["status"] == "ready"

    encoded = store.encode_document(doc)
    stored = json.loads(encoded)
    assert stored["artifact_bundle"]["artifacts"]["$dedup"] == "artifacts"
    assert stored["artifact_bundle"]["artifacts"]["status_overrides"]["0"] == "ready"

    expanded = store_body_dedup.expand(stored)
    assert expanded == doc


def test_dedup_leaves_divergent_copies_untouched(tmp_path):
    doc = _sample_result(tmp_path, "dedup03", with_status=True)
    doc["loop_outcome"]["trace_refs"]["execution_id"] = "different-exec"
    doc["artifact_bundle"]["artifacts"][0]["extra_custom_field"] = "keep-me"

    encoded = store.encode_document(doc)
    stored = json.loads(encoded)
    assert stored["loop_outcome"]["trace_refs"]["execution_id"] == "different-exec"
    assert isinstance(stored["artifact_bundle"]["artifacts"], list)
    assert stored["artifact_bundle"]["artifacts"][0]["extra_custom_field"] == "keep-me"
    assert store_body_dedup.expand(stored) == doc


def test_store_write_read_list_and_display_views_with_dedup(tmp_path):
    doc = _sample_result(tmp_path, "dedup04", with_status=True)
    write_task_result(
        tmp_path,
        "dedup04",
        "completed",
        result="done",
        artifacts=doc["artifacts"],
        artifact_bundle=doc["artifact_bundle"],
        trace_refs=doc["trace_refs"],
        loop_outcome=doc["loop_outcome"],
    )

    with store.open_store(tmp_path, write=False) as txn:
        raw_stored = txn.body_text("dedup04")
    assert raw_stored is not None
    parsed_raw = json.loads(raw_stored)
    assert parsed_raw["loop_outcome"]["trace_refs"] == {"$dedup": "trace_refs"}
    assert parsed_raw["artifact_bundle"]["artifacts"] == {"$dedup": "artifacts"}

    # Full readers reconstruct exact logical documents.
    loaded = load_task_result(tmp_path, "dedup04")
    assert loaded["loop_outcome"]["trace_refs"] == loaded["trace_refs"]
    assert len(loaded["artifact_bundle"]["artifacts"]) == 20
    listed = task_store.list_documents(tmp_path)
    assert len(listed) == 1 and len(listed[0]["artifact_bundle"]["artifacts"]) == 20
    by_ids = task_store.documents_by_ids(tmp_path, ["dedup04"])
    assert by_ids["dedup04"]["loop_outcome"]["trace_refs"] == by_ids["dedup04"]["trace_refs"]

    # SQL display projection keeps artifact_bundle.status and loop_outcome.usage.total_rounds.
    display = json.loads(task_store.display_documents(tmp_path, ["dedup04"])["dedup04"])
    assert display["artifact_bundle"] == {"status": "ready"}
    assert display["loop_outcome"] == {"usage": {"total_rounds": 15}}


def test_compact_and_expand_legacy_task_bodies_and_maintenance_rests(tmp_path):
    doc = _sample_result(tmp_path, "legacy01", with_status=True)
    write_task_result(
        tmp_path,
        "legacy01",
        "completed",
        result="done",
        artifacts=doc["artifacts"],
        artifact_bundle=doc["artifact_bundle"],
        trace_refs=doc["trace_refs"],
        loop_outcome=doc["loop_outcome"],
    )
    # Simulate a legacy undeduplicated row already sitting in task_bodies.
    before_full = load_task_result(tmp_path, "legacy01")
    with store.open_store(tmp_path, write=True) as txn:
        raw_before = store.document_text(before_full)
        txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id=?", (raw_before, "legacy01"))
        seq_before = txn.row("legacy01", "write_seq")

    report = maintenance.run_job(tmp_path, store_body_dedup.JOB, should_stop=lambda: False)
    assert report["state"] == "done"
    assert report["items"] == 1
    assert report["bytes_saved"] > 1000

    with store.open_store(tmp_path, write=False) as txn:
        raw_after = txn.body_text("legacy01")
        seq_after = txn.row("legacy01", "write_seq")
    assert seq_after == seq_before  # logical document unchanged
    assert len(raw_after) < len(raw_before)
    assert load_task_result(tmp_path, "legacy01") == before_full

    # Maintenance job rests immediately once done.
    assert store_body_dedup.JOB not in maintenance.due(tmp_path)

    # Inverse rollback helper expands cleanly back to full JSON.
    restored = store_body_dedup.expand_task_bodies(tmp_path)
    assert restored["expanded"] == 1
    with store.open_store(tmp_path, write=False) as txn:
        raw_restored = txn.body_text("legacy01")
    assert "$dedup" not in raw_restored
    assert load_task_result(tmp_path, "legacy01") == before_full


def test_collect_summarizes_bulk_and_test_evidence_dirs_skips_pycache_and_keeps_small_dirs(tmp_path):
    store_dir = artifacts.task_artifact_dir_path(tmp_path, "bulk01", create=True)
    # 1. Generated bytecode cache -> skipped completely
    pycache = store_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-314.pyc").write_bytes(b"\x00pyc")

    # 2. Small normal directory (<= 64 files) -> per-file relpaths preserved
    small = store_dir / "reports" / "a"
    small.mkdir(parents=True)
    (small / "summary.txt").write_text("small summary", encoding="utf-8")

    # 3. Test-evidence directory (even with < 64 files because of marker) -> single directory_manifest
    ev = store_dir / "full-evidence" / "run-1"
    ev.mkdir(parents=True)
    (ev / "OUROBOROS_TEST_EVIDENCE_RUN.txt").write_text("ok\n", encoding="utf-8")
    (ev / "trace.log").write_text("log\n", encoding="utf-8")

    # 4. Bulk directory (> 64 files) -> single directory_manifest
    bulk = store_dir / "qualification-full"
    bulk.mkdir()
    for i in range(artifacts.BULK_DIRECTORY_THRESHOLD + 5):
        (bulk / f"case_{i:03d}.json").write_text(f'{{"i": {i}}}', encoding="utf-8")

    # 5. delegated_runs -> always per-file even with > 64 files
    dr = store_dir / "delegated_runs" / "run-1"
    dr.mkdir(parents=True)
    (dr / "workspace.patch").write_text("diff\n", encoding="utf-8")

    rows = artifacts.collect_task_artifact_records(tmp_path, "bulk01")
    names = {row["name"]: row for row in rows}
    assert "mod.cpython-314.pyc" not in names
    assert "summary.txt" in names and names["summary.txt"]["relpath"] == "reports/a/summary.txt"
    assert "workspace.patch" in names and names["workspace.patch"]["relpath"] == "delegated_runs/run-1/workspace.patch"

    assert names["full-evidence"]["kind"] == "directory_manifest"
    assert names["full-evidence"]["file_count"] == 2
    assert names["full-evidence"]["is_directory"] is True

    assert names["qualification-full"]["kind"] == "directory_manifest"
    assert names["qualification-full"]["file_count"] == artifacts.BULK_DIRECTORY_THRESHOLD + 5
    assert len(names["qualification-full"]["sample_files"]) == 10
    assert len(rows) == 4  # summary.txt, workspace.patch, full-evidence, qualification-full


def test_directory_manifest_survives_child_copyback_and_archive_download(tmp_path):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    child_store = artifacts.task_artifact_dir_path(child, "kidbulk", create=True)
    ev = child_store / "full-evidence"
    ev.mkdir()
    (ev / "OUROBOROS_TEST_EVIDENCE_RUN.txt").write_text("pass\n", encoding="utf-8")
    (ev / "detail.json").write_text('{"ok": true}\n', encoding="utf-8")
    (ev / "__pycache__").mkdir()
    (ev / "__pycache__" / "junk.pyc").write_bytes(b"pyc")

    collected = artifacts.collect_task_artifact_records(child, "kidbulk")
    assert len(collected) == 1 and collected[0]["kind"] == "directory_manifest"
    write_task_result(child, "kidbulk", "completed", result="ok", artifacts=collected)
    write_task_result(parent, "kidbulk", "running", child_drive_root=str(child))

    copied = copy_child_task_result(parent, {"id": "kidbulk", "drive_root": str(child)})
    assert copied is not None
    effective = load_effective_task_result(parent, "kidbulk", materialize_artifacts=True)
    assert effective["artifact_status"] == "ready"
    manifest_rows = [r for r in effective["artifacts"] if r.get("kind") == "directory_manifest"]
    assert len(manifest_rows) == 1
    assert pathlib.Path(manifest_rows[0]["path"]).is_relative_to(parent)
    assert (artifacts.task_artifact_dir_path(parent, "kidbulk") / "full-evidence" / "detail.json").exists()
    assert not (artifacts.task_artifact_dir_path(parent, "kidbulk") / "full-evidence" / "__pycache__").exists()

    # Gateway directory archive listing & ZIP streaming still work for directory_manifest rows.
    stores = [artifacts.task_artifact_dir_path(parent, "kidbulk")]
    archives = task_archive.directory_archives(stores, effective["artifacts"], anchor=parent)
    assert "full-evidence" in archives
    assert archives["full-evidence"]["available"] is True
    assert archives["full-evidence"]["files"] == 2
    members = task_archive._archive_members(stores, effective["artifacts"], "full-evidence", anchor=parent)
    assert [m[0] for m in members] == ["full-evidence/OUROBOROS_TEST_EVIDENCE_RUN.txt", "full-evidence/detail.json"]


def test_dedup_handles_non_numeric_size_and_strict_json_key_order(tmp_path):
    doc = _sample_result(tmp_path, "edge01", with_status=True)
    # Non-numeric / boolean size must never raise in encode_document.
    doc["artifacts"][0]["size"] = "not-a-number"
    encoded = store.encode_document(doc)
    stored = json.loads(encoded)
    assert isinstance(stored["artifact_bundle"]["artifacts"], list)
    assert store_body_dedup.expand(stored) == doc

    # Key order difference / True vs 1 must never false-match.
    doc2 = _sample_result(tmp_path, "edge02", with_status=True)
    doc2["trace_refs"] = {"a": 1, "b": True}
    doc2["loop_outcome"]["trace_refs"] = {"b": 1, "a": True}
    stored2 = json.loads(store.encode_document(doc2))
    assert stored2["loop_outcome"]["trace_refs"] == {"b": 1, "a": True}


def test_copyback_rejects_escaping_directory_manifest_and_versions_existing_parent_files(tmp_path):
    from ouroboros import task_finalization

    parent = tmp_path / "parent"
    child = tmp_path / "child"
    outside = tmp_path / "outside_secret_dir"
    parent.mkdir()
    child.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("top-secret", encoding="utf-8")

    # 1. Escaping directory_manifest path MUST fail copy-back and never copy outside files.
    write_task_result(
        child,
        "escape01",
        "completed",
        result="ok",
        artifacts=[{
            "kind": "directory_manifest",
            "name": "outside_secret_dir",
            "path": str(outside),
            "is_directory": True,
            "file_count": 1,
            "size": 10,
            "sha256": "",
            "status": "ready",
            "errors": [],
        }],
    )
    write_task_result(parent, "escape01", "running", child_drive_root=str(child))
    copied = copy_child_task_result(parent, {"id": "escape01", "drive_root": str(child)})
    assert copied is not None
    assert copied["artifacts"][0]["copy_status"] == "failed"
    assert not (artifacts.task_artifact_dir_path(parent, "escape01", create=False) / "outside_secret_dir").exists()

    # 2. Existing differing parent file is version-archived before overwrite, and rescued count sums file_count.
    child_store = artifacts.task_artifact_dir_path(child, "ver01", create=True)
    ev = child_store / "full-evidence"
    ev.mkdir()
    (ev / "OUROBOROS_TEST_EVIDENCE_RUN.txt").write_text("run-2\n", encoding="utf-8")
    (ev / "report.json").write_text('{"v": 2}\n', encoding="utf-8")

    parent_ev = artifacts.task_artifact_dir_path(parent, "ver01", create=True) / "full-evidence"
    parent_ev.mkdir()
    (parent_ev / "report.json").write_text('{"v": 1}\n', encoding="utf-8")

    collected = artifacts.collect_task_artifact_records(child, "ver01")
    assert collected[0]["sha256"] == "" and collected[0]["tree_sha256"]
    assert task_finalization._listed_file_count("ver01", child_store) == 2

    write_task_result(child, "ver01", "completed", result="ok", artifacts=collected)
    write_task_result(parent, "ver01", "running", child_drive_root=str(child))
    copy_child_task_result(parent, {"id": "ver01", "drive_root": str(child)})
    assert (parent_ev / "report.json").read_text(encoding="utf-8") == '{"v": 2}\n'
    versions_dir = parent / "task_results" / "artifact_versions" / "ver01" / "report.json"
    assert versions_dir.is_dir() and any(p.read_text(encoding="utf-8") == '{"v": 1}\n' for p in versions_dir.iterdir())
