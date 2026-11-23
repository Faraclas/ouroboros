"""Tests for WI-3.2: 14-day rolling observability retention, holds, typed pruned_by_retention,
retired OUROBOROS_OBSERVABILITY_RETENTION_DAYS knob, no startup census/deletion, and Storage Free.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from ouroboros import (
    artifacts,
    evidence_packer,
    evidence_packs,
    maintenance,
    maintenance_jobs,
    observability,
    observability_retention,
    projects_registry,
    review_ledger,
    store,
    storage_free,
    usage_store,
)


def _seed_store(tmp_path):
    calls = tmp_path / "observability" / "calls" / "t1"
    blobs = tmp_path / "observability" / "blobs"
    calls.mkdir(parents=True)
    blobs.mkdir(parents=True)
    aged = time.time() - 4000 * 86400
    paths = [calls / "a.json", calls / "b.json", blobs / "x.gz", blobs / "y.gz", blobs / "z.gz"]
    for path in paths:
        path.write_bytes(b"data")
        os.utime(path, (aged, aged))
    return paths


def test_startup_neither_counts_nor_deletes_observability_blobs(tmp_path, monkeypatch):
    from ouroboros import server_maintenance as sm

    monkeypatch.setattr(sm, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OUROBOROS_OBSERVABILITY_RETENTION_DAYS", "1")  # inert: retired
    paths = _seed_store(tmp_path)
    touched = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        if "observability" in str(path):
            touched.append(str(path))
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as startup:
        startup.setattr(os, "stat", spy)
        if hasattr(pathlib, "_NormalAccessor"):
            startup.setattr(pathlib._NormalAccessor, "stat", staticmethod(spy))
        sm._startup_prune_sweeps()
    assert touched == [], touched
    assert all(path.read_bytes() == b"data" for path in paths)
    assert not hasattr(observability, "prune_observability_blobs")


def test_retention_knob_is_retired_everywhere():
    import inspect

    from ouroboros.settings_defaults import RETIRED_SETTING_KEYS

    for mod in (observability, observability_retention):
        source = inspect.getsource(mod)
        assert 'environ.get("OUROBOROS_OBSERVABILITY_RETENTION_DAYS"' not in source
    assert "OUROBOROS_OBSERVABILITY_RETENTION_DAYS" in RETIRED_SETTING_KEYS


# ---- Helpers for WI-3.2 retention tests -----------------------------------------------------


def _write_fresh_queue_snapshot(root: pathlib.Path, *, running: list = None) -> None:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    (state_dir / "queue_snapshot.json").write_text(
        json.dumps({"generation": 1, "ts": now_iso, "running": list(running or []), "pending": []}),
        encoding="utf-8",
    )
    (state_dir / "queue.alive").write_text(
        json.dumps({"generation": 1, "ts": now_iso, "heartbeats": {}}),
        encoding="utf-8",
    )


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _write_blob(root: pathlib.Path, raw: bytes, *, kind: str = "json", age_days: float = 0.0) -> dict:
    sha = hashlib.sha256(raw).hexdigest()
    path = root / "observability" / "blobs" / f"{sha}.{kind}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb", compresslevel=1) as fh:
        fh.write(raw)
    if age_days > 0:
        mt = time.time() - age_days * 86400
        os.utime(path, (mt, mt))
    return {"kind": kind, "encoding": "gzip", "sha256": sha, "size": len(raw), "path": str(path)}


def _write_task(root: pathlib.Path, task_id: str, *, status: str = "completed", age_days: float = 20.0, **extra) -> None:
    ts = _iso_days_ago(age_days)
    row = {
        "task_id": task_id,
        "status": status,
        "_schema_version": 1,
        "ts": ts,
        "updated_at": ts,
        "drive_retention_at": ts,
        **extra,
    }
    with store.open_store(root, write=True) as txn:
        txn.store_task(row)


def _seed_packed_task(
    root: pathlib.Path,
    task_id: str,
    *,
    age_days: float = 20.0,
    status: str = "completed",
    payload_text: str = "hello",
    **task_extra,
) -> dict:
    raw = json.dumps({"message": {"content": f"{payload_text}-{task_id}"}}).encode("utf-8")
    blob_ref = _write_blob(root, raw, kind="json", age_days=0.0)
    calls = root / "observability" / "calls" / task_id
    calls.mkdir(parents=True, exist_ok=True)
    call_id = f"llm_{task_id}_1"
    manifest_obj = {
        "schema_version": 1,
        "task_id": task_id,
        "call_id": call_id,
        "call_type": "llm_response",
        "full_payload_ref": blob_ref,
    }
    manifest_bytes = json.dumps(manifest_obj).encode("utf-8")
    manifest_path = calls / f"{call_id}.json"
    manifest_path.write_bytes(manifest_bytes)
    handle_ref = artifacts.store_actor_source_bytes(
        root,
        task_id,
        category="context_checkpoints",
        source_id=f"focus_{task_id}",
        data=f"checkpoint-{task_id}".encode("utf-8"),
        extension="md",
    )
    _write_task(root, task_id, status=status, age_days=age_days, **task_extra)
    return {
        "task_id": task_id,
        "call_id": call_id,
        "manifest_ref": {
            "path": str(manifest_path),
            "call_id": call_id,
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "blob_ref": blob_ref,
        "handle_ref": handle_ref,
    }


def test_14d_rolling_retention_prunes_packs_calls_blobs_and_surfaces_typed_pruned_by_retention(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)

    aged = _seed_packed_task(root, "task-aged", age_days=20.0)
    young = _seed_packed_task(root, "task-young", age_days=3.0)

    # Pack both now: pack .xz mtime is TODAY, proving pack age uses the task's settled timestamp!
    pack_report = evidence_packer.run_pass(root)
    assert pack_report["packed"] == 2 and pack_report["faults"] == 0
    assert (root / "observability" / "packs" / "task-aged.xz").is_file()
    assert (root / "observability" / "packs" / "task-young.xz").is_file()

    # Aged loose call manifest + preserved model_send_violation + preserved salvaged text
    loose_tid = "task-loose-aged"
    _write_task(root, loose_tid, status="completed", age_days=18.0)
    loose_calls = root / "observability" / "calls" / loose_tid
    loose_calls.mkdir(parents=True)
    aged_blob = _write_blob(root, b'{"orphan":"aged"}', kind="json", age_days=16.0)
    young_blob = _write_blob(root, b'{"orphan":"young"}', kind="json", age_days=2.0)
    old_call = loose_calls / "llm_old.json"
    old_call.write_text(json.dumps({"call_id": "llm_old", "task_id": loose_tid, "full_payload_ref": aged_blob}), encoding="utf-8")
    old_mt = time.time() - 18 * 86400
    os.utime(old_call, (old_mt, old_mt))
    violation = loose_calls / "att_1.model_send_violation.json"
    violation.write_text('{"violation":"test"}', encoding="utf-8")
    os.utime(violation, (old_mt, old_mt))
    salvaged_path = pathlib.Path(observability.preserve_salvaged_output(root, loose_tid, "partial output"))
    os.utime(salvaged_path, (old_mt, old_mt))

    # Run observability_retention via maintenance
    assert "observability_retention" in maintenance.due(root)
    res = maintenance.run_job(root, "observability_retention")
    assert res is not None and res["state"] == "done" and res["items"] >= 3

    # Aged pack + sidecar deleted AND evicted from index.sqlite
    assert not (root / "observability" / "packs" / "task-aged.xz").exists()
    assert not (root / "observability" / "packs" / "task-aged.members.json").exists()
    with evidence_packs.open_index(root, write=False, rebuild=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packs WHERE task_id='task-aged'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM packs WHERE task_id='task-young'").fetchone()[0] == 1

    # Young pack stays intact & readable
    assert (root / "observability" / "packs" / "task-young.xz").is_file()
    assert evidence_packs.blob_bytes(root, young["blob_ref"])

    # Aged loose call pruned; violation and salvaged preserved
    assert not old_call.exists()
    assert violation.is_file()
    assert salvaged_path.is_file()

    # Aged unreferenced loose blob pruned; young blob preserved
    assert not pathlib.Path(aged_blob["path"]).exists()
    assert pathlib.Path(young_blob["path"]).is_file()

    # Pruned manifest exists and is typed
    manifest = json.loads(observability_retention.pruned_manifest_path(root).read_text(encoding="utf-8"))
    assert manifest["status"] == "pruned_by_retention"
    assert "task-aged" in manifest["pruned_tasks"]
    assert aged["blob_ref"]["sha256"] in manifest["pruned_blobs"]
    assert aged_blob["sha256"] in manifest["pruned_blobs"]

    # Readers raise PrunedByRetentionError (subclass of FileNotFoundError), never EvidenceCorrupt
    with pytest.raises(evidence_packs.PrunedByRetentionError):
        evidence_packs.call_manifest_bytes(root, "task-aged", aged["call_id"])
    with pytest.raises(evidence_packs.PrunedByRetentionError):
        evidence_packs.manifest_bytes_by_sha(root, aged["manifest_ref"]["sha256"])
    with pytest.raises(evidence_packs.PrunedByRetentionError):
        evidence_packs.blob_bytes(root, aged["blob_ref"])
    with pytest.raises(observability.PrunedByRetentionError):
        observability.read_blob_ref(root, aged_blob)
    with pytest.raises(observability.PrunedByRetentionError):
        observability.read_call_manifest_ref(root, aged["manifest_ref"], task_id="task-aged")
    with pytest.raises(observability.PrunedByRetentionError):
        observability.read_call_payload(root, task_id="task-aged", call_id=aged["call_id"])
    with pytest.raises(evidence_packs.PrunedByRetentionError):
        evidence_packs.handle_bytes(root, "task-aged", aged["handle_ref"]["path"])

    # handle_info defaults to include_pruned=False (None) so store_actor_source_bytes doesn't skip writes!
    assert evidence_packs.handle_info(root, "task-aged", aged["handle_ref"]["path"]) is None
    pruned_info = evidence_packs.handle_info(root, "task-aged", aged["handle_ref"]["path"], include_pruned=True)
    assert pruned_info is not None and pruned_info["status"] == "pruned_by_retention"

    # query_data(op="blob") distinguishes pruned_by_retention from not_found
    from ouroboros.tools.query_data import _query_data
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=root, task_id="reader-task")
    raw_pruned = json.loads(_query_data(ctx, op="blob", sha256=aged_blob["sha256"], blob_kind="json"))
    assert raw_pruned["status"] == "unavailable"
    assert raw_pruned["code"] == "pruned_by_retention"
    assert raw_pruned["pruned"]["sha256"] == aged_blob["sha256"]

    raw_missing = json.loads(_query_data(ctx, op="blob", sha256="0" * 64, blob_kind="json"))
    assert raw_missing["status"] == "unavailable"
    assert raw_missing["code"] == "not_found"

    # Second pass rests cleanly
    assert "observability_retention" not in maintenance.due(root)
    assert maintenance.run_job(root, "observability_retention") is None

    # Advancing next_due past 14d (and aging task-young's settled timestamp) wakes due() and prunes task-young
    old_ts = _iso_days_ago(16.0)
    with store.open_store(root, write=True) as txn:
        txn.conn.execute("UPDATE tasks SET drive_retention_at=?, updated_at=?, ts=? WHERE task_id='task-young'", (old_ts, old_ts, old_ts))
    job_row = maintenance_jobs.job(root, "observability_retention") or {}
    job_row["next_due"] = time.time() - 10.0
    job_row["young"] = {"task-young": time.time() - 10.0}
    maintenance_jobs.record(root, "observability_retention", **job_row)
    assert "observability_retention" in maintenance.due(root)
    res_young = maintenance.run_job(root, "observability_retention")
    assert res_young is not None and res_young["items"] == 1
    assert not (root / "observability" / "packs" / "task-young.xz").exists()


def test_strict_holds_active_project_open_review_open_custody_unresolved_usage_and_unmetered_copilot(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)

    # 1. Active project hold
    proj = projects_registry.create_project(root, "Active Research")
    pid = proj["id"]
    _seed_packed_task(root, "task-active-proj", age_days=25.0, project_id=pid)

    # 2. Non-terminal task hold
    _seed_packed_task(root, "task-running", age_days=25.0, status="running")

    # 3. Open review hold (pending record in state/review_ledger/)
    _seed_packed_task(root, "task-open-review", age_days=25.0)
    rev_rec = review_ledger.ReviewLedgerRecord(
        record_id="rl-20261101T000000-abcdef123456",
        state=review_ledger.STATE_PENDING,
        ts=_iso_days_ago(25.0),
        task_id="task-open-review",
        root_task_id="task-open-review",
        surface="commit_gate",
        verdict={"aggregate": review_ledger.VERDICT_NOT_PERFORMED, "reason": "review_late_result_pending"},
    )
    review_ledger.write_record(root, rev_rec.to_dict())

    # 4. Open delegated custody hold
    _seed_packed_task(root, "task-open-custody", age_days=25.0)
    with store.open_store(root, write=True) as txn:
        txn.conn.execute(
            "INSERT INTO custody_runs (run_id, task_id, project_id, invocation_id, state, facts, updated_at, root_task_id, output_unread) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("run-held-1", "task-open-custody", "", "inv-1", "held",
             json.dumps({"run_id": "run-held-1", "task_id": "task-open-custody", "state": "held"}),
             _iso_days_ago(25.0), "task-open-custody", 0),
        )

    # 5. Unresolved usage hold + physical_failure.evidence_ref blob hold
    _seed_packed_task(root, "task-open-usage", age_days=25.0)
    pf_blob = _write_blob(root, b'{"error":"502"}', kind="json", age_days=25.0)
    # 6. Settled unmetered Copilot attempt (cost_final=0, pricing_known=0) MUST NOT be held!
    _seed_packed_task(root, "task-unmetered-copilot", age_days=25.0)

    with usage_store.hold(root, write=True) as utxn:
        utxn.conn.execute(
            "INSERT INTO attempts (attempt_id, revision, seq, seq_first, task_id, root_task_id, parent_task_id, state, pricing_known, cost_usd, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("att-open-1", 1, 1, 1, "task-open-usage", "task-open-usage", "", "unresolved", 0, "0",
             json.dumps({"physical_failure": {"evidence_ref": pf_blob}})),
        )
        utxn.conn.execute(
            "INSERT INTO attempts (attempt_id, revision, seq, seq_first, task_id, root_task_id, parent_task_id, state, settle_reason, pricing_known, cost_usd, cost_final, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("att-copilot-settled", 1, 2, 2, "task-unmetered-copilot", "task-unmetered-copilot", "", "settled", "completed", 0, "0", 0, "{}"),
        )

    # Pack all packable tasks first
    evidence_packer.run_pass(root)
    assert (root / "observability" / "packs" / "task-active-proj.xz").is_file()
    assert (root / "observability" / "packs" / "task-open-review.xz").is_file()
    assert (root / "observability" / "packs" / "task-open-custody.xz").is_file()
    assert (root / "observability" / "packs" / "task-open-usage.xz").is_file()
    assert (root / "observability" / "packs" / "task-unmetered-copilot.xz").is_file()

    # Run retention: ONLY task-unmetered-copilot is pruned; ALL holds keep their files!
    res = maintenance.run_job(root, "observability_retention")
    assert res is not None and res["state"] == "done" and res["items"] == 1
    assert not (root / "observability" / "packs" / "task-unmetered-copilot.xz").exists()
    assert (root / "observability" / "packs" / "task-active-proj.xz").is_file()
    assert (root / "observability" / "calls" / "task-running").is_dir()
    assert (root / "observability" / "packs" / "task-open-review.xz").is_file()
    assert (root / "observability" / "packs" / "task-open-custody.xz").is_file()
    assert (root / "observability" / "packs" / "task-open-usage.xz").is_file()
    assert pathlib.Path(pf_blob["path"]).is_file()

    # Rests while holds remain unchanged
    assert "observability_retention" not in maintenance.due(root)

    # Release active project hold (tombstone project) -> wakes due() and prunes task-active-proj
    projects_registry.begin_project_deletion(root, pid)
    # Deleting lifecycle still holds until tombstoned
    maintenance.run_job(root, "observability_retention")
    assert (root / "observability" / "packs" / "task-active-proj.xz").is_file()
    projects_registry.complete_project_deletion(root, pid)
    assert "observability_retention" in maintenance.due(root)
    res2 = maintenance.run_job(root, "observability_retention")
    assert res2 is not None and res2["items"] == 1
    assert not (root / "observability" / "packs" / "task-active-proj.xz").exists()

    # Settle pending review -> wakes due() and prunes task-open-review
    review_ledger.revise_record(
        root,
        rev_rec.record_id,
        lambda p: {**p, "state": review_ledger.STATE_SETTLED, "verdict": {"aggregate": review_ledger.VERDICT_PASS, "reason": ""}},
    )
    assert "observability_retention" in maintenance.due(root)
    res3 = maintenance.run_job(root, "observability_retention")
    assert res3 is not None and res3["items"] == 1
    assert not (root / "observability" / "packs" / "task-open-review.xz").exists()


def test_fail_closed_on_unreadable_authorities_and_rests_until_changed(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)

    _seed_packed_task(root, "task-aged-fc", age_days=25.0)
    evidence_packer.run_pass(root)
    assert (root / "observability" / "packs" / "task-aged-fc.xz").is_file()

    # Corrupt projects.json -> fail closed!
    proj_path = root / "state" / "projects.json"
    proj_path.parent.mkdir(parents=True, exist_ok=True)
    proj_path.write_text("{corrupt json", encoding="utf-8")

    res = maintenance.run_job(root, "observability_retention")
    assert res is not None and res["items"] == 0
    assert (root / "observability" / "packs" / "task-aged-fc.xz").is_file()
    row = maintenance_jobs.job(root, "observability_retention") or {}
    assert row.get("authority_unreadable") is True
    assert row.get("authority_reason") == "projects_registry_unreadable"

    # Rests until the corrupt file changes
    assert "observability_retention" not in maintenance.due(root)

    # Repair projects.json -> wakes and prunes!
    proj_path.write_text('{"schema_version": 1, "projects": []}', encoding="utf-8")
    assert "observability_retention" in maintenance.due(root)
    res2 = maintenance.run_job(root, "observability_retention")
    assert res2 is not None and res2["items"] == 1
    assert not (root / "observability" / "packs" / "task-aged-fc.xz").exists()

    # Corrupt review_ledger index -> fail closed!
    _seed_packed_task(root, "task-aged-rl", age_days=25.0)
    evidence_packer.run_pass(root)
    rdir = root / "state" / "review_ledger"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "index.jsonl").write_text("{bad json\n", encoding="utf-8")
    res3 = maintenance.run_job(root, "observability_retention")
    assert res3 is not None and res3["items"] == 0
    assert (root / "observability" / "packs" / "task-aged-rl.xz").is_file()
    assert (maintenance_jobs.job(root, "observability_retention") or {}).get("authority_reason") == "review_ledger_unreadable"
    assert "observability_retention" not in maintenance.due(root)
    (rdir / "index.jsonl").write_text("", encoding="utf-8")
    assert "observability_retention" in maintenance.due(root)
    res4 = maintenance.run_job(root, "observability_retention")
    assert res4 is not None and res4["items"] == 1
    assert not (root / "observability" / "packs" / "task-aged-rl.xz").exists()

    # Corrupt usage.sqlite -> fail closed!
    _seed_packed_task(root, "task-aged-usg", age_days=25.0)
    evidence_packer.run_pass(root)
    upath = root / usage_store.STORE_REL
    upath.write_bytes(b"not a sqlite db")
    res5 = maintenance.run_job(root, "observability_retention")
    assert res5 is not None and res5["items"] == 0
    assert (root / "observability" / "packs" / "task-aged-usg.xz").is_file()
    assert (maintenance_jobs.job(root, "observability_retention") or {}).get("authority_reason") == "usage_store_unreadable"
    upath.unlink()
    assert "observability_retention" in maintenance.due(root)
    res6 = maintenance.run_job(root, "observability_retention")
    assert res6 is not None and res6["items"] == 1
    assert not (root / "observability" / "packs" / "task-aged-usg.xz").exists()


def test_storage_free_aged_observability_preview_and_run(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))

    proj = projects_registry.create_project(root, "Held Project")
    _seed_packed_task(root, "task-free-aged", age_days=22.0)
    _seed_packed_task(root, "task-free-held", age_days=22.0, project_id=proj["id"])
    evidence_packer.run_pass(root)
    aged_blob = _write_blob(root, b'{"free":"blob"}', kind="json", age_days=20.0)
    observability_retention._merge_pruned_manifest(
        root, add_blobs={aged_blob["sha256"]: {"kind": "json", "task_id": "task-prior-pruned", "bytes": 16}}
    )

    owner = storage_free.FreeSpace(live=lambda _tid: False, offered=(storage_free.GROUP_AGED_OBSERVABILITY,))
    assert owner.preview(root, tmp_path) is True
    deadline = time.monotonic() + 10.0
    while owner.projection(root)["state"] == "listing" and time.monotonic() < deadline:
        time.sleep(0.02)
    proj_view = owner.projection(root)
    assert proj_view["state"] == "ready"
    [grp] = proj_view["groups"]
    assert grp["id"] == storage_free.GROUP_AGED_OBSERVABILITY
    assert grp["count"] == 2  # task-free-aged + unreferenced aged blobs
    assert grp["in_use"] == 1  # task-free-held
    assert grp["unreadable"] == 0
    assert grp["bytes"] and grp["bytes_complete"] is True

    started = owner.start(root, tmp_path, proj_view["plan"], [storage_free.GROUP_AGED_OBSERVABILITY])
    assert started == {"started": True, "reason": ""}
    deadline = time.monotonic() + 10.0
    while owner.projection(root)["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    done_view = owner.projection(root)
    assert done_view["state"] == "done"
    assert done_view["run"]["removed"] == 2
    assert not (root / "observability" / "packs" / "task-free-aged.xz").exists()
    assert not pathlib.Path(aged_blob["path"]).exists()
    assert (root / "observability" / "packs" / "task-free-held.xz").is_file()


def test_aged_loose_blob_referenced_by_surviving_pack_is_kept(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    seeded = _seed_packed_task(root, "task-young-pack", age_days=2.0)
    evidence_packer.run_pass(root)
    raw_packed = observability.read_call_payload(root, task_id="task-young-pack", call_id=seeded["call_id"])[0]["full_payload_ref"]
    blob_path = pathlib.Path(raw_packed["path"])
    if not blob_path.exists():
        from ouroboros.evidence_packs import blob_bytes
        import gzip
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(gzip.compress(blob_bytes(root, raw_packed)))
    aged_mtime = time.time() - 30 * 86400
    os.utime(blob_path, (aged_mtime, aged_mtime))
    maintenance.run_job(root, "observability_retention")
    assert blob_path.is_file()


def test_write_blob_refreshes_mtime_and_publications_protects_concurrent_sweep(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    payload = {"reused": "value"}
    ref = observability.write_blob(root, payload, kind="json")
    bpath = pathlib.Path(ref["path"])
    aged = time.time() - 30 * 86400
    os.utime(bpath, (aged, aged))
    assert bpath.stat().st_mtime < time.time() - 14 * 86400
    observability.write_blob(root, payload, kind="json")
    assert bpath.stat().st_mtime >= time.time() - 60

    # Even if an unreferenced blob is aged, publishing a call manifest referencing it during a sweep preserves it via .publications.
    race_blob = _write_blob(root, b'{"race":"live"}', kind="json", age_days=25.0)
    race_path = pathlib.Path(race_blob["path"])
    orig_scan = observability_retention._scan_aged_unreferenced_blobs
    published = []

    def simulate_concurrent_publish(*args, **kwargs):
        res = orig_scan(*args, **kwargs)
        if not published:
            published.append(True)
            observability.write_call_manifest(root, task_id="live-racer", call_id="c_race", manifest={"call_type": "tool_call", "full_payload_ref": race_blob})
        return res

    monkeypatch.setattr(observability_retention, "_scan_aged_unreferenced_blobs", simulate_concurrent_publish)
    holds = observability_retention._load_holds(root)
    cutoff = time.time() - 14 * 86400
    rem, _bytes, _mt, ok = observability_retention._prune_aged_unreferenced_blobs(root, holds=holds, cutoff_ts=cutoff)
    assert ok and rem == 0
    assert race_path.is_file()


def test_unattributed_service_archive_blob_is_preserved_across_retention_and_storage_free(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    svc_blob = _write_blob(root, b"service stdout archived log", kind="txt", age_days=30.0)
    svc_path = pathlib.Path(svc_blob["path"])
    maintenance.run_job(root, "observability_retention")
    assert svc_path.is_file()
    cands, _in_use, unr = observability_retention.list_aged_observability_candidates(root, live=lambda _t: False)
    assert unr == 0 and cands == []
    assert svc_path.is_file()


def test_long_lived_task_settled_today_keeps_older_loose_calls_until_14d_after_settlement(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    tid = "task-long-paused"
    _write_task(root, tid, status="completed", age_days=0.0)
    cdir = root / "observability" / "calls" / tid
    cdir.mkdir(parents=True)
    cfile = cdir / "llm_early.json"
    cfile.write_text(json.dumps({"call_id": "llm_early", "task_id": tid}), encoding="utf-8")
    old_mt = time.time() - 25 * 86400
    os.utime(cfile, (old_mt, old_mt))
    maintenance.run_job(root, "observability_retention")
    assert cfile.is_file()


def test_model_send_seal_audit_classifies_pruned_by_retention_without_violation(tmp_path, monkeypatch):
    from ouroboros import model_send_seal, usage_store
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    aged = _seed_packed_task(root, "task-sealed-pruned", age_days=20.0)
    evidence_packer.run_pass(root)
    extra = {
        "candidate_manifest_ref": {**aged["manifest_ref"], "model_send_seal_version": 1},
        "candidate_measurement_kind": "canonical_json_v1",
        "candidate_raw_sha256": "a" * 64,
        "candidate_raw_size_bytes": 10,
    }
    with usage_store.hold(root, write=True) as utxn:
        utxn.conn.execute(
            "INSERT INTO attempts (attempt_id, revision, seq, seq_first, task_id, root_task_id, parent_task_id, state, settle_reason, pricing_known, cost_usd, cost_final, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("att-sealed-1", 1, 1, 1, "task-sealed-pruned", "task-sealed-pruned", "", "settled", "completed", 0, "0", 1, json.dumps(extra)),
        )
    maintenance.run_job(root, "observability_retention")
    report = model_send_seal.reconcile_model_send_seals(root)
    assert report["unlogged_attempts"] == 0
    assert report["pruned_by_retention_attempts"] == 1


def test_due_rests_when_waiting_retry_set_and_child_false(tmp_path, monkeypatch):
    from ouroboros import maintenance_jobs
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    _write_fresh_queue_snapshot(root)
    _write_task(root, "task-done-retry", status="completed", age_days=1.0)
    (root / "observability" / "calls").mkdir(parents=True, exist_ok=True)
    maintenance.run_job(root, "observability_retention")
    with store.open_store(root, write=True) as txn:
        row = maintenance_jobs.job(root, "observability_retention") or {}
        maintenance_jobs.record_in(txn, "observability_retention", **{**row, "waiting_retry": ["task-done-retry"]})
    assert not observability_retention.due(root, maintenance_jobs.job(root, "observability_retention"), {"revision": 999999}, child=False)
    assert observability_retention.due(root, maintenance_jobs.job(root, "observability_retention"), {"revision": 999999}, child=True)
