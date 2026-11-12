"""Artifact identity survives the registered producer, storage and bounded consumers."""
from __future__ import annotations

import hashlib
import json

import pytest

from ouroboros import _outcome_receipts as R
from ouroboros import outcome_receipt_store as store
from ouroboros.review_evidence import _accept_verification_summary
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
def observer(tmp_path, monkeypatch):
    from ouroboros import safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **k: (True, ""))
    work = tmp_path / "work"
    work.mkdir()
    drive = tmp_path / "replica"
    drive.mkdir()
    core = tmp_path / "core"
    core.mkdir()
    ctx = ToolContext(repo_dir=core, system_repo_dir=core, drive_root=drive,
                      budget_drive_root=drive, workspace_root=work,
                      workspace_mode="external", task_id="artifact-identity")
    reg = ToolRegistry(work, drive)
    reg.set_context(ctx)
    for i in range(25):
        (work / f"common-{i:02}.txt").write_text("present", encoding="utf-8")
    (work / "present-tail.txt").write_text("present", encoding="utf-8")

    def observe(paths, **kwargs):
        return reg.execute_result("verify_and_record", {
            "contract_kind": "artifact_observation", "artifact_paths": paths, **kwargs,
        })

    return ctx, work, observe


@pytest.mark.parametrize("count", [19, 20, 25])
def test_full_set_survives_public_observation_union_and_review(observer, tmp_path, count):
    ctx, work, observe = observer
    common = [f"common-{i:02}.txt" for i in range(count)]
    failed = common + ["missing-tail.txt"]
    unrelated = common + ["present-tail.txt"]
    assert "FAIL" in observe(failed).text
    assert "OBSERVED" in observe(unrelated).text
    rows = store.read_verification_receipts(ctx.drive_root, ctx.task_id)
    assert rows[0]["paths"] == failed
    assert rows[1]["paths"] == unrelated
    assert all(r["paths_complete"] is True for r in rows)
    assert R.receipt_identity(rows[0]) != R.receipt_identity(rows[1])
    assert R.unreconciled_failed(rows) == [rows[0]]
    canonical = tmp_path / "canonical"
    assert store.append_verification_receipt(canonical, ctx.task_id, rows[1])
    assert store.publish_verification_receipt_union(canonical, ctx.task_id, ctx.drive_root)
    merged = store.read_verification_receipts_from_roots([canonical, ctx.drive_root], ctx.task_id)
    assert len(merged) == 2 and R.unreconciled_failed(merged) == [rows[0]]
    summary = _accept_verification_summary(merged)
    assert summary["unreconciled_red_count"] == 1
    projection = R.verification_receipt_ledger_row(rows[0])
    assert projection["paths_complete"] is True
    assert projection["paths_omitted"] == max(0, count + 1 - R.IDENTITY_PATH_LIMIT)
    assert projection["paths_identity_sha256"] == hashlib.sha256(
        R.receipt_canonical_identity(rows[0]).paths_identity_source.encode("utf-8")
    ).hexdigest()
    assert summary["unreconciled_red_identity"]["paths_identity_sha256"] == projection["paths_identity_sha256"]
    from ouroboros.outcomes import build_verification_ledger
    from ouroboros.review_evidence_sections import _accept_receipt_exhibits
    from ouroboros.tools.control_task_results import _subtask_outcome_summary

    ledger = build_verification_ledger(
        task={"id": ctx.task_id, "task_contract": {}}, loop_outcome={},
        llm_trace={"tool_calls": [], "verification_receipts": merged}, artifact_bundle={},
    )
    entries = [r for r in ledger["entries"] if r["kind"] == "verification_receipt"]
    assert len(entries) == 2 and entries[0]["paths_complete"] is True
    assert entries[0]["paths_identity_sha256"] == projection["paths_identity_sha256"]
    assert all(r["paths_complete"] is True for r in _accept_receipt_exhibits(merged))
    handoff = json.loads(_subtask_outcome_summary({"id": ctx.task_id}, merged))
    assert handoff["verification_receipts"][0]["outstanding"] == "unreconciled_failed"
    assert handoff["verification_receipts"][0]["paths_complete"] is True
    # Repair the SAME set; ordering and duplicates are not new identities.
    (work / "missing-tail.txt").write_text("fixed", encoding="utf-8")
    assert "OBSERVED" in observe(list(reversed(failed)) + failed[:2]).text
    current = store.read_verification_receipts(ctx.drive_root, ctx.task_id)
    assert R.unreconciled_failed(current) == []
    assert R.receipt_identity(current[0]) == R.receipt_identity(current[-1])


def test_complete_current_set_cannot_clear_a_truncated_legacy_set(observer):
    ctx, _, observe = observer
    common = [f"common-{i:02}.txt" for i in range(20)]
    legacy = {"status": "fail", "contract_kind": "artifact_observation", "paths": common}
    assert store.append_verification_receipt(ctx.drive_root, ctx.task_id, legacy)
    before = store.verification_receipts_path(ctx.drive_root, ctx.task_id).read_bytes()
    assert "OBSERVED" in observe(common).text
    rows = store.read_verification_receipts(ctx.drive_root, ctx.task_id)
    assert R.unreconciled_failed(rows) == [legacy]
    assert store.verification_receipts_path(ctx.drive_root, ctx.task_id).read_bytes().startswith(before)
    # Historical rows remain unversioned and compare as they did; no lost tail is invented.
    assert "paths_complete" not in R.receipt_identity_projection(legacy)
    assert R.receipt_identity(legacy) == ("artifact_paths", json.dumps(common))
    assert R.unreconciled_failed([legacy, dict(legacy, status="observed")]) == []
    for invalid in (False, None, 1, "true"):
        assert R.receipt_identity({**legacy, "paths_complete": invalid}) == R.receipt_identity(legacy)
    assert R.unreconciled_failed([dict(rows[-1], status="fail"), dict(legacy, status="observed")]) != []


def test_path_completeness_does_not_override_criterion_check_or_empty_identity():
    for fields, expected in [({"criterion_id": "c"}, "criterion_id"),
                             ({"check": "echo yes"}, "check"), ({}, "artifact_paths")]:
        old = {"status": "fail", "paths": ["a"], **fields}
        new = {**old, "status": "observed", "paths_complete": True}
        assert R.receipt_identity(new)[0] == expected
        assert bool(R.unreconciled_failed([old, new])) == (expected == "artifact_paths")
    assert R.receipt_identity({"paths": [], "paths_complete": True}) == ("none", "")
    assert R.unreconciled_failed([{"status": "fail", "paths": []},
                                 {"status": "observed", "paths_complete": True}]) == []
    for paths in (["a\nb"], ["a", "b"], ["a "], ["a"]):
        assert R.receipt_canonical_identity({"paths": paths, "paths_complete": True}).paths == tuple(paths)
    assert R.receipt_identity({"paths": ["a\nb"], "paths_complete": True}) != R.receipt_identity({"paths": ["a", "b"], "paths_complete": True})


def test_refusal_and_receipt_custody_stay_distinct(observer, monkeypatch):
    ctx, _, observe = observer
    forbidden = ctx.drive_root / "state" / "state.json"
    forbidden.parent.mkdir()
    forbidden.write_text("{}", encoding="utf-8")
    assert "REFUSED_OUT_OF_SCOPE" in observe([str(forbidden)]).text
    rows = store.read_verification_receipts(ctx.drive_root, ctx.task_id)
    assert rows[-1]["status"] == "refused_out_of_scope"
    assert R.unreconciled_failed(rows) == []
    original = store.verification_receipts_path(ctx.drive_root, ctx.task_id).read_bytes()
    monkeypatch.setattr("ouroboros.tools.verify.append_verification_receipt", lambda *a, **k: False)
    result = observe(["present-tail.txt"])
    assert "RECEIPT" in result.text.upper() and "OBSERVED:" not in result.text
    assert store.verification_receipts_path(ctx.drive_root, ctx.task_id).read_bytes() == original
