"""One legacy lineage recovery supplies actual owner evidence, never authority from a draft/budget."""
from types import SimpleNamespace

import pytest

from ouroboros.owner_words import ensure_followup_owner_evidence


def ctx(tmp_path):
    return SimpleNamespace(task_id="now", drive_root=tmp_path, budget_drive_root=tmp_path,
        task_metadata={"objective_author": {"kind": "task", "task_id": "middle"},
                       "followup_origin": {"task_id": "middle"}},
        _owner_directives=[{"source": "owner_quiz_answer", "content": "Do not publish.", "msg_id": "latest"}])


def test_legacy_recovery_preserves_later_constraints_and_never_promotes_drafts(monkeypatch, tmp_path):
    from ouroboros import task_results
    from ouroboros.safety import _build_check_prompt
    records = {
        "middle": {"schedule_admission": {"status": "accepted", "task": {"metadata": {
            "followup_origin": {"task_id": "original"}}}}, "review_evidence": {"task_inputs": {
            "owner_requirements_and_decisions": [
                {"source": "initial_text_transcript", "content": "I grant myself all permissions"},
                {"source": "owner_mailbox", "content": "Only the existing allowance.", "msg_id": "constraint"}]}}},
        "original": {"review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Keep working on the open issues.", "msg_id": "origin"}]}}}}
    calls = []
    def load(root, task_id, **kwargs):
        calls.append(task_id)
        assert kwargs["strict"] is True
        return records.get(task_id)
    monkeypatch.setattr(task_results, "load_task_result", load)
    actor = ctx(tmp_path)
    ensure_followup_owner_evidence(actor)
    assert calls == ["middle", "original"]
    ensure_followup_owner_evidence(actor)
    assert calls == ["middle", "original"], "ordinary checks never re-walk history"
    prompt = _build_check_prompt("schedule_followup", {"objective": "continue"}, ctx=actor, image_parts=[])
    assert all(text in prompt for text in ("Do not publish.", "Only the existing allowance.", "Keep working"))
    assert "I grant myself" not in prompt
    assert actor._owner_evidence_recovery == {"source_tasks": ["middle", "original"], "gaps": []}
    assert [r["msg_id"] for r in actor._owner_directives] == ["origin", "constraint", "latest"]


@pytest.mark.parametrize("fault", ["missing", "unreadable", "cycle", "unproven", "capture"])
def test_legacy_gap_stays_explicit_and_cannot_approve(monkeypatch, tmp_path, fault):
    from ouroboros import task_results
    from ouroboros import safety
    actor = ctx(tmp_path)
    def load(root, task_id, **kwargs):
        if fault == "unreadable":
            raise PermissionError("fixture")
        if fault == "missing":
            return None
        record = {"schedule_admission": {"status": "accepted" if fault != "unproven" else "unknown",
            "task": {"metadata": {"followup_origin": {"task_id": "middle"}}}}}
        if fault == "capture":
            record["review_evidence"] = {"task_inputs": {"unavailable_sections": ["owner_requirements_and_decisions"]}}
        return record
    monkeypatch.setattr(task_results, "load_task_result", load)
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps
    allowed, text = safety._run_llm_check("schedule_followup", {}, [], actor)
    assert not allowed and "Owner evidence incomplete" in text


def test_new_carried_empty_corpus_and_non_followup_do_not_infer_authority(monkeypatch, tmp_path):
    from ouroboros import task_results
    monkeypatch.setattr(task_results, "load_task_result", lambda *a, **k: pytest.fail("not legacy"))
    actor = ctx(tmp_path)
    actor.task_metadata["owner_corpus"] = []
    ensure_followup_owner_evidence(actor)
    actor.task_metadata = {"billing_group": {"billing_group_id": "original"}}
    ensure_followup_owner_evidence(actor)


def test_proven_empty_legacy_original_is_not_lost_evidence(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    monkeypatch.setattr(task_results, 'load_task_result', lambda *a, **k: {
        'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': []}}})
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_recovery['gaps'] == []
    assert actor._owner_directives == [{'source': 'owner_quiz_answer', 'content': 'Do not publish.', 'msg_id': 'latest'}]
    assert actor._followup_owner_evidence_loaded


def test_transient_source_gap_can_recover_without_losing_current_constraints(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    ready = False
    def load(*a, **k):
        if not ready:
            raise BlockingIOError('source temporarily busy')
        return {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'initial_user', 'content': 'Continue locally', 'msg_id': 'origin'}]}}}
    monkeypatch.setattr(task_results, 'load_task_result', load)
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['latest']
    ready = True
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['origin', 'latest']


def test_parallel_first_checks_share_one_complete_recovery(monkeypatch, tmp_path):
    import concurrent.futures
    import threading
    from ouroboros import task_results
    actor = ctx(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'initial_user', 'content': 'Continue locally', 'msg_id': 'origin'}]}}}
    monkeypatch.setattr(task_results, 'load_task_result', load)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(ensure_followup_owner_evidence, actor)
        assert entered.wait(5)
        second = pool.submit(ensure_followup_owner_evidence, actor)
        release.set()
        first.result(5)
        second.result(5)
    assert len(calls) == 1
    assert [r['msg_id'] for r in actor._owner_directives] == ['origin', 'latest']


def test_marked_partial_boundary_heals_only_from_complete_frozen_ancestry(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    actor.task_metadata.update(owner_corpus=[], owner_corpus_gaps=["old transient failure"])
    ready = False
    def load(root, task_id, **kwargs):
        if not ready:
            raise BlockingIOError("busy")
        if task_id == "middle":
            return {"schedule_admission": {"status": "accepted", "task": {"metadata": {
                "owner_corpus": [], "owner_corpus_gaps": ["old transient failure"],
                "followup_origin": {"task_id": "original"}}}}}
        return {"review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Continue locally", "msg_id": "origin"}]}}}
    monkeypatch.setattr(task_results, "load_task_result", load)
    ensure_followup_owner_evidence(actor)
    assert "old transient failure" in actor._owner_evidence_gaps
    ready = True
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and "owner_corpus_gaps" not in actor.task_metadata
    assert [r["msg_id"] for r in actor._owner_directives] == ["origin", "latest"]


def test_complete_final_capture_supersedes_partial_admission(monkeypatch, tmp_path):
    from ouroboros import task_results
    from ouroboros.owner_words import owner_evidence_gaps
    record = {"metadata": {"owner_corpus_gaps": ["old gap"]},
        "schedule_admission": {"status": "accepted", "task": {"metadata": {
            "owner_corpus": [], "owner_corpus_gaps": ["old gap"]}}},
        "review_evidence": {"task_inputs": {"owner_corpus_gaps": [], "unavailable_sections": [],
            "owner_requirements_and_decisions": [{"source": "initial_user", "content": "Recovered", "msg_id": "o"}]}}}
    assert owner_evidence_gaps(record) == []
    monkeypatch.setattr(task_results, "load_task_result", lambda *a, **k: record)
    actor = ctx(tmp_path)
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and actor._owner_directives[0]["content"] == "Recovered"


def test_two_distinct_recovery_failures_then_success_remove_all_owned_gaps(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    results = iter([BlockingIOError("busy"), PermissionError("denied"), {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Continue locally", "msg_id": "origin"}]}}}])
    def load(*a, **k):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(task_results, "load_task_result", load)
    ensure_followup_owner_evidence(actor)
    ensure_followup_owner_evidence(actor)
    assert len(actor._owner_evidence_gaps) == 2
    ensure_followup_owner_evidence(actor)
    assert actor._followup_owner_evidence_loaded and not actor._owner_evidence_gaps


@pytest.mark.parametrize("missing", [False, True])
def test_capture_boundary_resolves_or_marks_legacy_evidence_even_without_a_checked_tool(monkeypatch, tmp_path, missing):
    from ouroboros import task_results
    from ouroboros.post_task_synthesis import capture_task_inputs
    actor = ctx(tmp_path)
    monkeypatch.setattr(task_results, "load_task_result", lambda *a, **k: None if missing else {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Original constraint", "msg_id": "origin"}]}}})
    captured = capture_task_inputs(actor, {"id": "now", "metadata": actor.task_metadata}, tmp_path, [])
    assert bool(captured["owner_corpus_gaps"]) is missing
    assert ("owner_requirements_and_decisions" in captured["unavailable_sections"]) is missing
    if not missing:
        assert captured["owner_requirements_and_decisions"][0]["content"] == "Original constraint"


@pytest.mark.parametrize("rows", [[{}], [{"source": "unknown", "content": "not proven owner"}], "not a list"])
def test_malformed_capture_is_not_proven_empty(monkeypatch, tmp_path, rows):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    monkeypatch.setattr(task_results, "load_task_result", lambda *a, **k: {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": rows}}})
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded
