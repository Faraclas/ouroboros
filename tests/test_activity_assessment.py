"""Judgment changes emphasis only for the exact retained source, not a workflow heuristic."""
import pytest
from ouroboros.activity_history import activity_sources, record_assessment
from ouroboros.task_results import write_task_result, load_task_result


def failed(root):
    write_task_result(root, "failed-task", "failed", title="A real failed task", reason_code="runtime_failure")
    return load_task_result(root, "failed-task")


def test_failed_task_is_error_but_assessment_does_not_change_outcome(tmp_path):
    entry = activity_sources(failed(tmp_path))[0]
    assert entry["error"] is True
    judgment = record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"],
                                 "Inspected the failure and assigned source investigation",
                                 actor={"kind": "ouroboros", "task_id": "integrator"},
                                 investigating_task_id="investigation")
    current = load_task_result(tmp_path, "failed-task")
    assert current["status"] == "failed"
    assert activity_sources(current)[0]["assessment"] == judgment
    assert judgment["by"]["task_id"] == "integrator"
    assert judgment["investigating_task_id"] == "investigation"


def test_no_inferred_assessment_from_continue_or_new_task(tmp_path):
    row = failed(tmp_path)
    row.update(continued_by="new-task", result="fixed", elapsed_sec=999999,
               followup_schedules=["investigation"], memory_marks=["seen"], viewed=True)
    assert "assessment" not in activity_sources(row)[0]


@pytest.mark.parametrize("changed", [
    {"error": "A different runtime exception"},
    {"failure": {"kind": "runtime", "detail": "A different execution cause"}},
    {"outcome_axes": {"execution": {"status": "infra_failed", "reasons": ["new_cause"]}}},
])
def test_same_phase_changed_failure_facts_withhold_judgment(tmp_path, changed):
    entry = activity_sources(failed(tmp_path))[0]
    record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"], "Evaluated original cause",
                      actor={"kind": "ouroboros", "task_id": "integrator"})
    write_task_result(tmp_path, "failed-task", "failed", **changed)
    shown = activity_sources(load_task_result(tmp_path, "failed-task"))[0]
    assert shown["source_hash"] != entry["source_hash"]
    assert "assessment" not in shown


def test_source_change_withholds_old_judgment_and_refuses_stale_write(tmp_path):
    entry = activity_sources(failed(tmp_path))[0]
    record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"], "Evaluated old failure",
                      actor={"kind": "ouroboros", "task_id": "integrator"})
    write_task_result(tmp_path, "failed-task", "failed", reason_code="different_failure")
    assert "assessment" not in activity_sources(load_task_result(tmp_path, "failed-task"))[0]
    with pytest.raises(ValueError, match="Activity changed"):
        record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"], "Stale",
                          actor={"kind": "ouroboros", "task_id": "integrator"})


def test_nonzero_service_exit_and_owner_stop_are_not_automatically_errors():
    row = {"task_id": "stopped", "status": "cancelled", "cancel_origin": {"stop_policy": "immediate"},
           "service_observations": {"services": [{"service_id": "stopped:s", "started_at": "2026-01-01T00:00:00Z",
                                                   "name": "deliberate exit", "state": "exited", "returncode": 7}]}}
    assert all(not e["error"] for e in activity_sources(row))
    row["service_observations"]["services"][0]["returncode"] = -9
    assert all(not e["error"] for e in activity_sources(row))


def test_unknown_task_writes_nothing(tmp_path):
    with pytest.raises(LookupError):
        record_assessment(tmp_path, "missing", "task:missing", "x", "Saw no task",
                          actor={"kind": "ouroboros", "task_id": "integrator"})
    assert load_task_result(tmp_path, "missing") is None


def test_replica_enrichment_preserves_canonical_judgment(tmp_path):
    entry = activity_sources(failed(tmp_path))[0]
    saved = record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"], "Canonical judgment",
                              actor={"kind": "ouroboros", "task_id": "integrator"})
    write_task_result(tmp_path, "failed-task", "failed", activity_assessments={entry["key"]: {"reason": "stale copy"}})
    assert activity_sources(load_task_result(tmp_path, "failed-task"))[0]["assessment"] == saved


def test_failed_check_has_its_own_immutable_source_and_assessment():
    row = {"task_id": "working", "status": "running"}
    receipts = [
        {"ts": "2026-01-01T00:00:00Z", "status": "fail", "criterion_id": "check-one", "returncode": 0},
        {"ts": "2026-01-01T00:01:00Z", "status": "pass", "criterion_id": "check-one", "returncode": 0}]
    entries = activity_sources(row, receipts)
    assert len(entries) == 2
    assert entries[0]["error"] is False
    assert entries[1]["kind"] == "check" and entries[1]["error"] is True
    assert "assessment" not in entries[1]


def test_real_receipt_file_and_execution_replica_feed_history_and_assessment(tmp_path):
    from ouroboros.activity_history import activity_receipts, room_activity_page
    from ouroboros.outcome_receipt_store import append_verification_receipt
    from tests._store_shared import seed_result
    replica = tmp_path / "execution"
    seed_result(tmp_path, "checking", {"task_id": "checking", "status": "running", "chat_id": 1,
                                      "child_drive_root": str(replica), "title": "Checks"})
    receipt = {"ts": "2026-01-01T00:00:00Z", "status": "fail", "criterion_id": "real-check",
               "contract_kind": "explicit_command", "returncode": 0, "matched": False}
    assert append_verification_receipt(replica, "checking", receipt)
    row = load_task_result(tmp_path, "checking")
    gaps = set()
    receipts = activity_receipts(tmp_path, row, gaps)
    assert receipts == [receipt] and not gaps
    check = activity_sources(row, receipts)[1]
    assert check["error"] is True
    saved = record_assessment(tmp_path, "checking", check["key"], check["source_hash"], "Compared the actual output",
                              actor={"kind": "ouroboros", "task_id": "integrator"})
    assert append_verification_receipt(replica, "checking", {**receipt, "status": "pass", "ts": "2026-01-01T00:01:00Z"})
    page = room_activity_page(tmp_path, 1)
    entry = next(e for e in page["tasks"][0]["entries"] if e["kind"] == "check")
    # A later PASS is verification reconciliation, not evaluation of this historical occurrence.
    assert entry["error"] is True and entry["assessment"] == saved
    assert entry["at"] == receipt["ts"]


def test_corrupt_real_receipt_stream_reports_gap_and_refuses_check_judgment(tmp_path):
    from ouroboros.activity_history import activity_receipts
    from ouroboros.outcome_receipt_store import verification_receipts_path, append_verification_receipt
    row = failed(tmp_path)
    receipt = {"ts": "2026-01-01T00:00:00Z", "status": "fail", "criterion_id": "check"}
    assert append_verification_receipt(tmp_path, "failed-task", receipt)
    path = verification_receipts_path(tmp_path, "failed-task")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken}\n")
    gaps = set()
    entries = activity_sources(row, activity_receipts(tmp_path, row, gaps))
    assert gaps and entries[1]["error"]
    with pytest.raises(ValueError, match="incomplete"):
        record_assessment(tmp_path, "failed-task", entries[1]["key"], entries[1]["source_hash"], "Unobserved",
                          actor={"kind": "ouroboros", "task_id": "integrator"})


def test_explicit_judgment_can_identify_a_service_error_without_exit_code_heuristic(tmp_path):
    from tests._store_shared import seed_result
    row = seed_result(tmp_path, "service-owner", {"task_id": "service-owner", "status": "completed",
        "service_observations": {"services": [{"service_id": "svc", "started_at": "2026-01-01T00:00:00Z",
                                               "state": "exited", "returncode": 1}]}})
    entry = activity_sources(row)[1]
    assert not entry["error"]
    record_assessment(tmp_path, "service-owner", entry["key"], entry["source_hash"], "Read log: unexpected test failure",
                      actor={"kind": "ouroboros", "task_id": "integrator"}, declared_error=True)
    shown = activity_sources(load_task_result(tmp_path, "service-owner"))[1]
    assert shown["error"] and shown["assessment"]["declared_error"]
    assert shown["service"]["returncode"] == 1


def test_canonical_judgment_survives_effective_replica_overlay(tmp_path):
    from ouroboros.post_task_checkpoint import project_replica_task_result_fields
    entry = activity_sources(failed(tmp_path))[0]
    judgment = record_assessment(tmp_path, "failed-task", entry["key"], entry["source_hash"], "Canonical",
                                 actor={"kind": "ouroboros", "task_id": "integrator"})
    canonical = load_task_result(tmp_path, "failed-task")
    overlay = project_replica_task_result_fields(canonical, {**canonical,
        "activity_assessments": {entry["key"]: {"reason": "old worker snapshot"}}})
    assert overlay["activity_assessments"][entry["key"]] == judgment


def test_a_synthetic_task_receipt_field_is_not_evidence():
    row = {"task_id": "not-real", "status": "running", "verification_receipts": [{"status": "fail"}]}
    assert len(activity_sources(row)) == 1
