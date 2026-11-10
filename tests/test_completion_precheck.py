"""Evidence used by the author-side pre-result-review checklist.

These tests exercise existing evidence owners, not an automatic review gate or a
model's future compliance. Bounded author-decision exercises can use the same
missing/submitted/read fixtures; their model outputs belong to task evidence.
"""
from __future__ import annotations

import pytest

from ouroboros.owner_mailbox import (
    acknowledge_task_messages,
    mail_read_state,
    mail_write_receipt,
    write_task_message,
)
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.verify import _verify_and_record


def test_exact_final_pointer_not_an_existing_draft(tmp_path):
    from ouroboros.outcomes import read_verification_receipts

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive = tmp_path / "drive"
    ctx = ToolContext(repo_dir=workspace, drive_root=drive, task_id="author")
    (workspace / "draft.md").write_text("earlier evidence", encoding="utf-8")
    final = workspace / "final.md"

    missing = _verify_and_record(
        ctx, contract_kind="artifact_observation", artifact_paths=[str(final)])
    assert "FAIL" in missing
    assert read_verification_receipts(drive, "author")[-1]["status"] == "fail"
    final.write_text("final evidence", encoding="utf-8")
    present = _verify_and_record(
        ctx, contract_kind="artifact_observation", artifact_paths=[str(final)])
    assert "OBSERVED" in present
    assert read_verification_receipts(drive, "author")[-1]["status"] == "observed"


@pytest.mark.parametrize("status,ended", [
    ("scheduled", False), ("running", False), ("running", True),
])
def test_submitted_receipt_never_claims_receiver_read(status, ended):
    receipt = mail_write_receipt(status, drain_ended=ended)
    assert receipt["read"] is False
    assert receipt["receipt"] == (
        "retained_unread" if ended else "queued" if status == "scheduled" else "delivered")


def test_missing_handoff_then_submission_then_transcript_consumption(tmp_path):
    from ouroboros.owner_mailbox import drain_owner_entries

    recipient, message = "recipient", "required-handoff"
    assert drain_owner_entries(tmp_path, recipient) == []
    assert mail_read_state(tmp_path, recipient, message) is False
    assert write_task_message(
        tmp_path, "The final source and report are ready", task_id=recipient,
        source_task_id="author", provenance="ancestor_task", msg_id=message)
    assert mail_read_state(tmp_path, recipient, message) is False
    [entry] = drain_owner_entries(tmp_path, recipient)
    assert entry["msg_id"] == message
    # Draining alone is not the ACK: full transcript injection owns this call.
    assert mail_read_state(tmp_path, recipient, message) is False
    assert acknowledge_task_messages(tmp_path, recipient, [message], wake_id="test-transcript")
    assert mail_read_state(tmp_path, recipient, message) is True
    # This boolean carries consumption, not a substantive recipient answer.
    assert entry["text"] == "The final source and report are ready"


def test_author_checklist_is_not_the_result_reviewers_rubric():
    from ouroboros.tools.review_checklist import load_checklist_section
    from ouroboros.tools.review_result import result_rubric

    author = load_checklist_section("Pre-Result-Review Self-Check")
    reviewer = result_rubric()
    assert author.startswith("## Pre-Result-Review Self-Check\n")
    assert "## Result Review Checklist" not in author
    assert reviewer.startswith("## Result Review Checklist\n")
    assert "Pre-Result-Review Self-Check" not in reviewer
