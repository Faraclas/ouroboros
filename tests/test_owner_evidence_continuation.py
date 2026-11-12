"""Recovered constraints compose with saved cognition, never replace each other."""
import copy
import json
from types import SimpleNamespace

import pytest

from ouroboros.loop_messages import (
    _initialize_owner_directives, _record_owner_directive, owner_reply_owed,
    owner_source_sha256,
)
from ouroboros.owner_wait import continuation_state, restore_continuation_state
from ouroboros.owner_words import ensure_followup_owner_evidence
from ouroboros.post_task_synthesis import capture_task_inputs
from ouroboros.review_evidence_sections import _accept_owner_directives
from ouroboros.safety import _build_check_prompt, _run_llm_check
from ouroboros.tools.control_routing import _attach_drafted_objective

ORIGINAL = {"source": "initial_user", "content": "  Original: do not publish. café\n", "msg_id": "origin"}
LATER = {"source": "owner_quiz_answer", "content": "Later: disposable data only.", "msg_id": "later", "quiz_id": "q"}
NOISE = [{"role": "user", "content": "Task draft, not owner words."},
         {"role": "user", "content": "[CONTEXT_FACTS] synthetic clock"}]


def actor(root, metadata):
    return SimpleNamespace(task_id="same", task_attempt=2, task_metadata=copy.deepcopy(metadata),
        drive_root=root, budget_drive_root=root, messages=copy.deepcopy(NOISE),
        task_contract={}, pending_events=[], event_queue=None, drive_logs=lambda: root / "logs")


def save(ctx):
    return json.loads(json.dumps(continuation_state(ctx, ctx.messages, {}, {}, 1, [], set())))


def restore(ctx, saved):
    restore_continuation_state(SimpleNamespace(_ctx=ctx), saved, ctx.messages, {}, {}, set())


def readers(ctx):
    prompt = _build_check_prompt("schedule_followup", {"objective": "fixture"}, messages=ctx.messages, ctx=ctx)
    occasion = json.JSONDecoder().raw_decode(prompt.split("Host evidence (provenance is not consent):\n", 1)[1])[0]["occasion"]
    captured = capture_task_inputs(ctx, {"id": ctx.task_id}, ctx.drive_root, [])
    routed = {}
    _attach_drafted_objective(ctx, routed)
    return occasion, _accept_owner_directives(ctx, None, ""), captured, routed


@pytest.mark.parametrize("carried", [None, [], [LATER]])
def test_fresh_recovery_and_saved_rows_survive_same_id_restore(tmp_path, monkeypatch, carried):
    from ouroboros import task_results
    from ouroboros.task_results import write_task_result
    # Real recorded/captured ancestor, availability controlled only at its read.
    ancestor = actor(tmp_path, {})
    _record_owner_directive(ancestor, **ORIGINAL)
    capture = capture_task_inputs(ancestor, {"id": "ancestor"}, tmp_path, [])
    write_task_result(tmp_path, "ancestor", "completed", review_evidence={"task_inputs": capture})
    original_load = task_results.load_task_result
    ready = False
    def load(root, tid, **kwargs):
        if tid == "ancestor" and not ready:
            raise PermissionError("fixture unavailable before save")
        return original_load(root, tid, **kwargs)
    monkeypatch.setattr(task_results, "load_task_result", load)
    metadata = {"objective_author": {"kind": "task"}, "followup_origin": {"task_id": "ancestor"}}
    if carried is not None:
        metadata.update(owner_corpus=carried, owner_corpus_gaps=["admission source missing"])
    old = actor(tmp_path, metadata)
    _initialize_owner_directives(old, old.messages)
    assert old._owner_evidence_gaps
    saved = save(old)
    # Old serializers erased absence; exercising that old state is intentional.
    if carried is None:
        saved["owner_directives"] = []
    ready = True
    current = actor(tmp_path, metadata)
    _initialize_owner_directives(current, current.messages)
    expected = [ORIGINAL, LATER] if carried else [ORIGINAL]
    assert current._owner_directives == expected
    restore(current, saved)
    ensure_followup_owner_evidence(current)
    assert current._owner_directives == expected
    assert current._followup_owner_evidence_loaded and not current._owner_evidence_gaps
    assert not owner_reply_owed(current), "recovered historical authority is not new dialogue"
    occasion, review, final, routed = readers(current)
    assert occasion["sources"] == review == expected
    assert final["owner_requirements_and_decisions"] == routed["owner_corpus"] == expected
    assert final["owner_corpus_gaps"] == [] and not routed.get("owner_corpus_gaps")
    assert "owner_requirements_and_decisions" not in final["unavailable_sections"]
    digest = owner_source_sha256(current)
    restore(current, json.loads(json.dumps(save(current))))
    assert current._owner_directives == expected and owner_source_sha256(current) == digest
    _record_owner_directive(current, source="owner_mailbox", content="A real new question", msg_id="new")
    assert owner_reply_owed(current)


def test_absent_new_checkpoint_remains_absent_and_explicit_empty_stays_empty(tmp_path):
    missing = actor(tmp_path, {"objective_author": {"kind": "task"}})
    _initialize_owner_directives(missing, missing.messages)
    saved = save(missing)
    assert saved["owner_directives"] is None
    restore(missing, saved)
    assert not isinstance(getattr(missing, "_owner_directives", None), list)
    empty = actor(tmp_path, {"objective_author": {"kind": "task"}, "owner_corpus": []})
    _initialize_owner_directives(empty, empty.messages)
    restore(empty, save(empty))
    assert readers(empty)[0]["sources"] == []


def test_unavailable_source_stays_a_gap_after_save_restore(tmp_path, monkeypatch):
    from ouroboros import task_results
    def missing(*a, **k):
        raise PermissionError("still missing")
    monkeypatch.setattr(task_results, "load_task_result", missing)
    metadata = {"objective_author": {"kind": "task"}, "followup_origin": {"task_id": "ancestor"},
                "owner_corpus": [LATER], "owner_corpus_gaps": ["admission gap"]}
    old = actor(tmp_path, metadata)
    _initialize_owner_directives(old, old.messages)
    current = actor(tmp_path, metadata)
    _initialize_owner_directives(current, current.messages)
    restore(current, save(old))
    allowed, reason = _run_llm_check("schedule_followup", {}, current.messages, current)
    assert not allowed and "Owner evidence incomplete" in reason
    assert not current._followup_owner_evidence_loaded
    assert readers(current)[2]["owner_corpus_gaps"]


def test_saved_complete_rows_survive_metadata_gap_pop(tmp_path, monkeypatch):
    from ouroboros import task_results
    metadata = {"objective_author": {"kind": "task"}, "followup_origin": {"task_id": "ancestor"},
                "owner_corpus": [LATER], "owner_corpus_gaps": ["old gap"]}
    monkeypatch.setattr(task_results, "load_task_result", lambda *_a, **_k: {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [ORIGINAL], "owner_corpus_gaps": []}}})
    old = actor(tmp_path, metadata)
    _initialize_owner_directives(old, old.messages)
    assert "owner_corpus_gaps" not in old.task_metadata
    saved = save(old)
    # Even if the popped metadata is used for restart, the successful capture
    # saved BOTH rows. No stale completeness block is necessary in cognition.
    current = actor(tmp_path, old.task_metadata)
    _initialize_owner_directives(current, current.messages)
    restore(current, saved)
    assert current._owner_directives == [ORIGINAL, LATER]


def test_working_wrapper_composes_evidence_without_replaying_unknown_call(tmp_path, monkeypatch):
    from ouroboros import owner_wait, task_pacing, budget_pause
    from ouroboros.working_checkpoint import resume_from_working
    metadata = {"objective_author": {"kind": "task"}, "owner_corpus": [ORIGINAL]}
    old = actor(tmp_path, metadata)
    _initialize_owner_directives(old, old.messages)
    _record_owner_directive(old, source=LATER["source"], content=LATER["content"],
                            msg_id=LATER["msg_id"], origin={"quiz_id": "q"})
    old.messages.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "unconfirmed", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]})
    saved = save(old)
    saved["_working_handoff"] = {"source_task_id": "same", "cause": "restart", "boundary": "post_batch"}
    # A later carried constraint is independently present at startup; its source
    # must compose with the older cognition rather than replace it.
    current = actor(tmp_path, {**metadata, "owner_corpus": [ORIGINAL,
        {"source": "owner_mailbox", "content": "Newest carried condition", "msg_id": "new-carried"}]})
    _initialize_owner_directives(current, current.messages)
    monkeypatch.setattr(owner_wait, "rebind_restored_route", lambda *_: (None, "max"))
    monkeypatch.setattr(task_pacing, "restore_cost_ceiling", lambda *_: None)
    monkeypatch.setattr(budget_pause, "observe_task_runs", lambda *_a, **_k: {"custody_read": "ok", "runs": []})
    result = resume_from_working(SimpleNamespace(_ctx=current), saved, current.messages, {}, {}, set())
    assert result[-1] is True
    assert {row["msg_id"] for row in current._owner_directives} == {"origin", "later", "new-carried"}
    unknown = [row for row in current.messages if row.get("tool_call_id") == "unconfirmed"]
    assert len(unknown) == 1 and "UNKNOWN" in unknown[0]["content"] and "NOT re-executed" in unknown[0]["content"]
    assert owner_reply_owed(current), "the saved unanswered quiz still owes a reply"
    occasion, review, final, routed = readers(current)
    for corpus in (occasion["sources"], review, final["owner_requirements_and_decisions"], routed["owner_corpus"]):
        assert {row["msg_id"] for row in corpus} == {"origin", "later", "new-carried"}


def test_inherited_anonymous_rows_are_idempotent_but_live_repeated_words_are_not(tmp_path):
    row = {"source": "owner_corpus", "content": "Exact repeated condition"}
    current = actor(tmp_path, {"objective_author": {"kind": "task"}, "owner_corpus": [row]})
    _initialize_owner_directives(current, current.messages)
    digest = owner_source_sha256(current)
    _initialize_owner_directives(current, current.messages)
    restore(current, save(current))
    assert current._owner_directives == [row] and owner_source_sha256(current) == digest
    _record_owner_directive(current, source="owner_mailbox", content=row["content"])
    _record_owner_directive(current, source="owner_mailbox", content=row["content"])
    assert len(current._owner_directives) == 3 and owner_reply_owed(current)


def test_saved_media_row_and_reply_mark_survive_fresh_duplicate(tmp_path):
    old = actor(tmp_path, {})
    media = [{"path": "retained.png", "sha256": "exact-retained-digest", "caption": "owner plan"}]
    _record_owner_directive(old, source="owner_mailbox", content="A host projection", owner_text="Original words",
                            media=media, msg_id="image")
    old._owner_reply_revision = 1
    saved = save(old)
    current = actor(tmp_path, {"objective_author": {"kind": "task"}, "owner_corpus": [
        {"source": "owner_mailbox", "content": "A host projection", "msg_id": "image"}]})
    _initialize_owner_directives(current, current.messages)
    restore(current, saved)
    assert current._owner_directives == old._owner_directives
    assert current._owner_directives[0]["media"] == media
    assert current._owner_directives[0]["owner_text"] == "Original words"
    assert owner_source_sha256(current) == owner_source_sha256(old) and not owner_reply_owed(current)


def test_late_presence_answer_is_live_even_in_task_authored_input(tmp_path):
    metadata = {"objective_author": {"kind": "task"}, "owner_corpus": []}
    old = actor(tmp_path, metadata)
    _initialize_owner_directives(old, old.messages)
    saved = save(old)
    block = {"quiz_id": "q", "state": "answered", "answered_after_terminal": True,
             "presence_contact": True, "comment": "New owner answer"}
    current = actor(tmp_path, {**metadata, "late_answer": {"task_id": "asker", "quiz_id": "q",
        "answer_snapshot": {"task_id": "asker", "quiz": block}}})
    _initialize_owner_directives(current, current.messages)
    restore(current, saved)
    assert current._owner_directives == [{"source": "owner_quiz_answer", "content": "New owner answer",
        "source_task_id": "asker", "quiz_id": "q"}]
    assert owner_reply_owed(current) and current._owner_inherited_revision == 0


def test_legacy_saved_reply_coordinates_keep_live_debt_when_fresh_rows_overlap(tmp_path):
    old = actor(tmp_path, {})
    _record_owner_directive(old, source="owner_mailbox", content="Saved unanswered", msg_id="live")
    old._owner_reply_revision = 0
    saved = save(old)
    saved["delivery"].pop("_owner_inherited_revision", None)
    current = actor(tmp_path, {"objective_author": {"kind": "task"}, "owner_corpus": [ORIGINAL,
        {"source": "owner_mailbox", "content": "Saved unanswered", "msg_id": "live"}]})
    _initialize_owner_directives(current, current.messages)
    before = owner_source_sha256(old)
    restore(current, saved)
    assert current._owner_directives == [ORIGINAL, saved["owner_directives"][0]]
    assert owner_source_sha256(current) != before
    assert current._owner_inherited_revision == 1 and owner_reply_owed(current)


@pytest.mark.parametrize("recover_first", [True, False])
def test_anonymous_carried_row_and_recovered_copy_are_one_source(tmp_path, monkeypatch, recover_first):
    from ouroboros import task_results
    anonymous = {"source": "initial_user", "content": "Exact anonymous original"}
    metadata = {"objective_author": {"kind": "task"}, "followup_origin": {"task_id": "ancestor"},
                "owner_corpus": [anonymous], "owner_corpus_gaps": ["old gap"]}
    ready = recover_first
    def load(*_a, **_k):
        if not ready:
            raise PermissionError("fixture initially unavailable")
        return {"review_evidence": {"task_inputs": {
            "owner_requirements_and_decisions": [anonymous], "owner_corpus_gaps": []}}}
    monkeypatch.setattr(task_results, "load_task_result", load)
    current = actor(tmp_path, metadata)
    _initialize_owner_directives(current, current.messages)
    ready = True
    ensure_followup_owner_evidence(current)
    assert len(current._owner_directives) == 1
    assert current._owner_directives[0]["content"] == anonymous["content"]
    assert current._owner_inherited_revision == 1 and not owner_reply_owed(current)
    digest = owner_source_sha256(current)
    _initialize_owner_directives(current, current.messages)
    restore(current, save(current))
    assert len(current._owner_directives) == 1 and owner_source_sha256(current) == digest
