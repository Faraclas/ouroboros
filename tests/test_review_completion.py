"""Completion is an authored episode operation, not a provider turn-end marker."""

import copy
import dataclasses
import json

import pytest

from ouroboros import review_native_episode as native
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.review_completion import (
    COMPLETION_REMINDER, SUBMIT_REVIEW_SCHEMA, legacy_review_answer, review_submission,
)
from tests.test_native_review_context_facts import _executor, _tool_result
from tests.test_native_tool_round_executor import _ScriptedLLM, _tool_call, _VERDICT


def _submit(answer, call_id="submit"):
    return {"tool_calls": [_tool_call("submit_review", {"answer": answer}, call_id)]}


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_real_normalizers_commentary_read_then_submission_continue_one_episode(tmp_path, provider):
    """Both real wire normalizers can return end-turn prose before another tool round."""
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="fixture-not-live")
    commentary = "Now ARCHITECTURE.md."

    class WireReviewer(_ScriptedLLM):
        def chat(self, **kwargs):
            message, _ = super().chat(**kwargs)
            index = len(self.calls)
            if provider == "openai":
                normalized, usage = client._normalize_remote_response({
                    "id": f"response-{index}",
                    "choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 9, "cost": 0.0},
                }, {"provider": "openai", "resolved_model": "fixture-openai"}, skip_cost_fetch=True)
            else:
                blocks = ([{"type": "text", "text": message["content"]}] if message.get("content") else [])
                blocks += [{"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                            "input": json.loads(call["function"]["arguments"])} for call in message.get("tool_calls", [])]
                normalized, usage = client._normalize_anthropic_response({
                    "content": blocks, "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }, {"provider": "anthropic", "resolved_model": "fixture-anthropic"})
            usage["ledger_attempt_ids"] = [f"attempt-{index}"]
            return normalized, usage

    llm = WireReviewer([
        {"content": commentary},
        {"tool_calls": [_tool_call("read_file", {"path": "source.txt"}, "read")]},
        _submit(_VERDICT),
    ])
    executor = _executor(tmp_path, llm)
    executor.assignment = dataclasses.replace(executor.assignment,
        slot=dataclasses.replace(executor.assignment.slot, model=f"{provider}::fixture"))
    result = executor.execute()
    assert result.raw_text == _VERDICT
    assert result.usage["native_end_reason"] == "submitted_answer"
    assert result.usage["native_rounds"] == 3 and not llm.script
    assert result.usage["native_tool_calls"] == len(result.usage["native_tool_receipts"]) == 2
    assert result.usage["native_tool_receipts"][-1] == {
        "round": 3, "tool": "submit_review", "outcome": "submitted", "delivered": False, "answer_chars": len(_VERDICT)}
    assert result.usage["ledger_attempt_ids"] == ["attempt-1", "attempt-2", "attempt-3"]
    assert "last source fact" in _tool_result(llm.calls[2], "read")
    assert any(m.get("content") == commentary for m in llm.calls[1]["messages"])
    assert any(m.get("content") == COMPLETION_REMINDER for m in llm.calls[1]["messages"])
    assert all(call["tool_choice"] == "auto" for call in llm.calls)
    for call in llm.calls:
        assert SUBMIT_REVIEW_SCHEMA in call["tools"]
    first = json.loads(read_actor_source_bytes(executor.assignment.custody_root, "t-native", executor._round_source_refs[0]))
    assert first["messages"][0]["content"] == commentary
    assert first["response_facts"]["ledger_attempt_ids"] == ["attempt-1"]
    if provider == "openai":
        assert first["response_facts"]["response_finish_reason"] == "stop"
    else:
        assert first["response_facts"]["stop_reason"] == "end_turn"


@pytest.mark.parametrize("shape, text", [
    ("array", "[]"), ("array", _VERDICT),
    ("array", '{"findings": []}'),
    ("array", '{"findings": ' + _VERDICT + '}'),
    ("array", '```json\n{"findings": []}\n```'),
    ("array", "[]\nNO_FINDINGS"),
    ("two_part", _VERDICT),
    ("two_part", '{"change": [], "change_clean": true, "coupling": []}'),
    ("array", '```json\n' + _VERDICT + '\n```'),
    ("two_part", '```json\n{"change": [], "change_clean": true, "coupling": []}\n```'),
    ("plan_reply", '```json\n{"prose": "Finished", "objections": [], "withdrawn": []}\n```'),
    ("plan_reply", '{"prose": "I object", "objections": [{"id": "x", "text": "unresolved"}], "withdrawn": []}'),
])
def test_legacy_whole_contract_answer_remains_byte_identical(tmp_path, shape, text):
    assert legacy_review_answer(text, shape)
    llm = _ScriptedLLM([{"content": text}])
    executor = _executor(tmp_path, llm, sources=False)
    executor.assignment.request.policy["output_shape"] = shape
    result = executor.execute()
    assert result.message["native_transcript"] == text and result.usage["native_rounds"] == 1
    # Fenced legacy answers may still use the existing post-episode extraction;
    # completion compatibility must not silently change that formatting policy.
    assert result.usage["native_end_reason"] == "final_answer"


@pytest.mark.parametrize("shape, text", [
    ("array", "I have not reviewed yet. Example: " + _VERDICT),
    ("array", 'Contract example:\n```json\n[]\n```'),
    ("array", 'Not reviewed yet; example {"findings": []}'),
    ("array", '{"findings": "not an array"}'),
    ("two_part", 'I will inspect next. {"findings": []}'),
    ("plan_reply", 'The contract says {"prose": "example"}'),
    ("plan_reply", '{"example": {"prose": "quoted"}}'),
    ("plan_reply", '{"prose": 7}'),
    ("report", '# Report draft\nMore evidence is needed.'),
])
def test_incidental_json_and_report_prose_leave_episode_open(tmp_path, shape, text):
    assert not legacy_review_answer(text, shape)
    answer = '{"prose": "Finished", "objections": [], "withdrawn": []}' if shape == "plan_reply" else (
        "# Exact final report" if shape == "report" else _VERDICT)
    llm = _ScriptedLLM([{"content": text}, _submit(answer)])
    executor = _executor(tmp_path, llm, sources=False)
    executor.assignment.request.policy["output_shape"] = shape
    result = executor.execute()
    assert result.message["native_transcript"] == answer
    assert result.usage["native_rounds"] == 2
    assert any(m.get("content") == text for m in llm.calls[-1]["messages"])


def test_no_commentary_count_limit(tmp_path):
    commentary = "Still investigating."
    llm = _ScriptedLLM([{"content": commentary}] * 45 + [_submit(_VERDICT)])
    result = _executor(tmp_path, llm, sources=False).execute()
    assert result.usage["native_rounds"] == 46 and not llm.script
    assert sum(m.get("content") == commentary for m in llm.calls[-1]["messages"]) == 45


@pytest.mark.parametrize("raw", ["{}", '{"answer": 1}', '{"answer": null}', '[]',
                                 '{"answer": "[]", "extra": true}', '"not an object"', "not json", None])
def test_bad_submit_arguments_return_error_then_allow_reviewer_next_action(tmp_path, raw):
    bad = _tool_call("submit_review", {}, "bad")
    bad["function"]["arguments"] = raw
    llm = _ScriptedLLM([
        {"tool_calls": [bad]},
        {"tool_calls": [_tool_call("read_file", {"path": "source.txt"}, "read")]},
        _submit(_VERDICT),
    ])
    executor = _executor(tmp_path, llm)
    result = executor.execute()
    assert "TOOL_ARG_ERROR" in _tool_result(llm.calls[1], "bad")
    assert "last source fact" in _tool_result(llm.calls[2], "read")
    assert result.usage["native_tool_receipts"][0]["outcome"] == "refused"
    assert result.raw_text == _VERDICT and len(llm.calls) == 3


@pytest.mark.parametrize("calls", [
    [_tool_call("submit_review", {"answer": "[]"}, "s1"), _tool_call("read_file", {"path": "source.txt"}, "read")],
    [_tool_call("read_file", {"path": "source.txt"}, "read"), _tool_call("submit_review", {"answer": "[]"}, "s1")],
    [_tool_call("submit_review", {"answer": "[]"}, "s1"), _tool_call("submit_review", {"answer": "[]"}, "s2"),
     _tool_call("read_file", {"path": "source.txt"}, "read")],
])
def test_mixed_or_multiple_submit_never_drops_other_calls(tmp_path, calls):
    llm = _ScriptedLLM([{"content": "Not a submission yet.", "tool_calls": calls}, _submit(_VERDICT)])
    result = _executor(tmp_path, llm).execute()
    returned = {m["tool_call_id"]: m["content"] for m in llm.calls[1]["messages"] if m.get("role") == "tool"}
    assert len(returned) == len(calls)
    assert "last source fact" in returned["read"]
    for name in set(returned) - {"read"}:
        assert "TOOL_ARG_ERROR" in returned[name] and "sole tool call" in returned[name]
    assert result.raw_text == _VERDICT and result.usage["native_read_coverage"]["status"] == "complete"


def test_wire_validation_refusal_applies_to_submit_without_dropping_history(tmp_path):
    class Refused:
        allows_execution = False
        error = "wire grammar refused"

    call = _submit("[]")["tool_calls"]
    answer, error = review_submission(call, {"submit": Refused()})
    assert answer is None and error == "wire grammar refused"
    answer, error = review_submission(_submit("[]")["tool_calls"], {})
    assert answer == "[]" and not error


@pytest.mark.parametrize("answer", ["not a verdict", "I refuse to review", "", "  exact whitespace\n"])
def test_explicit_submission_selects_exact_answer_before_downstream_parse(tmp_path, monkeypatch, answer):
    seen = []

    def canonicalize(text, **kwargs):
        seen.append((text, kwargs["shape"]))
        return text, "unparsed", {}

    monkeypatch.setattr(native, "canonicalize_session_verdict", canonicalize)
    llm = _ScriptedLLM([{"content": "My commentary", **_submit(answer)}])
    executor = _executor(tmp_path, llm, sources=False)
    result = executor.execute()
    assert seen == [(answer, "two_part")]
    assert result.message["native_transcript"] == answer
    assert result.usage["native_end_reason"] == "submitted_answer"
    assert len(llm.calls) == 1 and not llm.script
    executor.execute()  # format repair reuses the already-selected answer
    assert len(llm.calls) == 1 and seen == [(answer, "two_part"), (answer, "two_part")]


def test_report_empty_submission_does_not_replace_authored_answer_with_commentary(tmp_path):
    llm = _ScriptedLLM([{"content": "unfinished draft", **_submit("")}])
    executor = _executor(tmp_path, llm, sources=False)
    executor.assignment.request.policy["output_shape"] = "report"
    result = executor.execute()
    assert result.raw_text == "" and result.message["native_transcript"] == ""
    assert result.usage["native_end_reason"] == "submitted_answer"
    assert not result.usage.get("native_incomplete")


@pytest.mark.parametrize("ending", ["empty", "deadline", "budget", "control", "provider"])
def test_existing_failure_rails_still_end_after_commentary(tmp_path, monkeypatch, ending):
    from ouroboros.review_execution import ReviewRouteUnavailable
    from ouroboros.usage_accounting import BudgetExceeded, ControlAuthorityUnavailable

    errors = {"budget": BudgetExceeded("fixture budget"), "control": ControlAuthorityUnavailable("fixture control"),
              "provider": RuntimeError("fixture transport")}

    class Ends(_ScriptedLLM):
        def chat(self, **kwargs):
            if self.calls and ending in errors:
                raise errors[ending]
            return super().chat(**kwargs)

    llm = Ends([{"content": "Now the next file."}, {"content": ""}])
    executor = _executor(tmp_path, llm, sources=False)
    if ending == "deadline":
        monkeypatch.setattr(native, "owner_deadline_exhausted", lambda **_: executor._rounds_used >= 1)
    if ending == "empty":
        result = executor.execute()
        assert result.raw_text == "" and result.usage["native_end_reason"] == "empty_answer"
    else:
        with pytest.raises(ReviewRouteUnavailable if ending == "deadline" else type(errors[ending])):
            executor.execute()
        assert executor.failure_custody()["native_end_reason"] == {
            "deadline": "deadline_exhausted", "budget": "budget_exhausted",
            "control": "control_authority_unavailable", "provider": "transport_error",
        }[ending]
    first = json.loads(read_actor_source_bytes(executor.assignment.custody_root, "t-native", executor._round_source_refs[0]))
    assert first["messages"][0]["content"] == "Now the next file."


def test_compaction_keeps_completion_schema_and_native_delivery_instruction(tmp_path):
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "source.txt"}, "read")]},
        {"tool_calls": [_tool_call("compact_context", {"working_note": "Read complete source.", "keep_unit_ids": [],
                                                    "schema_names": ["read_file"]}, "compact")]},
        _submit(_VERDICT),
    ])
    executor = _executor(tmp_path, llm)
    executor.execute()
    assert executor._view_receipt["status"] == "applied"
    assert llm.calls[-1]["tools"] == llm.calls[0]["tools"]
    assert SUBMIT_REVIEW_SCHEMA in llm.calls[-1]["tools"]
    assert "Finish by calling submit_review alone" in llm.calls[-1]["messages"][0]["content"]
    assert "submit_review" not in native._INSPECTION_TOOL_NAMES
    before = copy.deepcopy(SUBMIT_REVIEW_SCHEMA)
    llm.calls[-1]["tools"][-1]["function"]["description"] = "fixture mutation"
    assert SUBMIT_REVIEW_SCHEMA == before  # each episode owns its schema copy
