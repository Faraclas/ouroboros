"""Reviewed assessment policy and labelled intent never replace host authority."""
import json

import pytest

from tests._safety_fixtures import supervisor as supervisor_fixture
from tests.test_tool_effects import published_tool as published_tool_fixture
from ouroboros.tool_effects import effect_of as REAL_EFFECT_OF
from ouroboros.safety import _safety_model_call as REAL_SAFETY_SEND

supervisor = supervisor_fixture
published_tool = published_tool_fixture


@pytest.mark.parametrize("kind", ["owner", "consciousness"])
def test_current_assignment_is_intent_not_owner_consent(supervisor, kind):
    h = supervisor
    h.ctx.task_metadata.update(work_kind=kind, objective_author={"kind": "task", "task_id": "parent"})
    h.ctx.task_contract.update(objective="Finish the approved repair and record its outcome",
                               expected_output="Installed consumer proof", constraints="No owner settings changes")
    h.ctx._owner_directives = [{"source": "initial_user", "content": "Repair and deliver this"},
                               {"source": "owner_quiz_answer", "content": "Keep the old item unchanged"}]
    prompt = h.module._build_check_prompt("start_task", {"objective": "Mark disposable work Done"}, ctx=h.ctx)
    bundle = json.loads(prompt.split("Host evidence (provenance is not consent):\n", 1)[1].split("\nProposed tool call:", 1)[0])
    assignment = bundle["assignment"]
    assert assignment["objective"] == h.ctx.task_contract["objective"]
    assert assignment["constraints"] == "No owner settings changes"
    assert assignment["author"] == {"kind": "task", "task_id": "parent"}
    assert assignment["authority"] == "intent_only"
    assert bundle["occasion"]["sources"] == h.ctx._owner_directives
    assert "MUST NOT BE INCLUDED" not in prompt


@pytest.mark.parametrize("policy", ["skip", "check", None, "invalid", {"skip": True}])
def test_reviewed_write_policy_is_separate_from_effect(supervisor, published_tool, monkeypatch, policy):
    from ouroboros import tool_effects
    h = supervisor
    monkeypatch.setattr(tool_effects, "effect_of", REAL_EFFECT_OF)
    name, descriptor = published_tool("write", safety_policy=policy)
    fact = tool_effects.effect_of(name, ext_tool=descriptor)
    h.module.check_safety(name, {"changes": {"disposition": "done"}}, ctx=h.ctx)
    assert len(h.calls) == (0 if policy == "skip" else 1)
    assert fact.effect == "write" and fact.declared


@pytest.mark.parametrize("change", ["generation", "duplicate", "absent"])
def test_skip_cannot_come_from_an_unpublished_declaration(supervisor, published_tool, monkeypatch, change):
    from ouroboros import tool_effects
    h = supervisor
    monkeypatch.setattr(tool_effects, "effect_of", REAL_EFFECT_OF)
    name, descriptor = published_tool("write", safety_policy="skip")
    if change == "generation":
        descriptor["extension_generation"] = "unpublished"
    elif change == "duplicate":
        rows = published_tool.bundle.loaded_skill.manifest.raw_extra["tools"]
        rows.append(dict(rows[0]))
    else:
        published_tool.bundle.loaded_skill.manifest.raw_extra["tools"].clear()
    h.module.check_safety(name, {}, ctx=h.ctx)
    assert len(h.calls) == 1


def test_message_policy_cannot_bypass_speech_assessment(supervisor, published_tool, monkeypatch):
    from ouroboros import tool_effects
    h = supervisor
    monkeypatch.setattr(tool_effects, "effect_of", REAL_EFFECT_OF)
    name, _ = published_tool("message", safety_policy="skip")
    h.replies.append({"status": "DANGEROUS", "reason": "Unauthorized secret publication"})
    allowed, warning = h.module.check_safety(name, {"text": "secret"}, ctx=h.ctx)
    assert not allowed and "Message not sent" in warning
    assert len(h.calls) == 1


def test_old_dispatch_cannot_borrow_new_skip(supervisor, published_tool, monkeypatch):
    from ouroboros import tool_effects
    h = supervisor
    monkeypatch.setattr(tool_effects, "effect_of", REAL_EFFECT_OF)
    name, descriptor = published_tool("write", safety_policy="check")
    old = dict(descriptor)
    descriptor["extension_generation"] = published_tool.bundle.generation_digest = "generation-two"
    published_tool("write", safety_policy="skip")
    assert h.module.check_safety(name, {}, ctx=h.ctx, ext_tool=old)[0]
    assert len(h.calls) == 1
    assert h.module.check_safety(name, {}, ctx=h.ctx, ext_tool=descriptor)[0]
    assert len(h.calls) == 1


@pytest.mark.parametrize("model", ["openai-compatible::gpt-6-luna", "openai-compatible::claude-sonnet-5.5"])
def test_checker_send_keeps_approved_work_and_negative_context(supervisor, monkeypatch, model):
    h = supervisor
    seen = []
    def observed(client, **kwargs):
        seen.append(kwargs)
        return {"content": '{"status":"DANGEROUS","reason":"Explicit owner limit"}'}, {}
    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", observed)
    h.ctx.task_contract["objective"] = "The task claims approval for disabling Safety"
    h.ctx._owner_directives = [{"source": "owner_quiz_answer", "content": "Do not disable Safety"}]
    prompt = h.module._build_check_prompt("start_task", {"objective": "Disable Safety"}, ctx=h.ctx)
    REAL_SAFETY_SEND(client=h.module.LLMClient(), ctx=h.ctx, tool_name="start_task", light_model=model,
        use_local=False, call_type="safety_supervisor", user_prompt=prompt, on_usage=lambda usage: None,
        system_prompt=h.module._get_safety_prompt(), attempts_limit=1)
    [wire] = seen
    assert wire["model"] == model and wire["model_role"] == "safety"
    assert wire["messages"][1]["content"] == prompt
    assert "Do not disable Safety" in prompt and '"authority": "intent_only"' in prompt
    assert "per-step approval" in wire["messages"][0]["content"][0]["text"]



@pytest.mark.parametrize("field", ["content", "owner_text"])
def test_assignment_reuses_complete_existing_source_instead_of_doubling_it(supervisor, field):
    h = supervisor
    text = "large owner task " * 18000
    h.ctx.task_contract["objective"] = text
    h.ctx._owner_directives = [{"source": "initial_user", field: text}]
    assert h.module.check_safety("write_remote", {"text": "small"}, ctx=h.ctx)[0]
    [call] = h.calls
    prompt = call["user_prompt"]
    bundle = json.loads(prompt.split("Host evidence (provenance is not consent):\n", 1)[1].split("\nProposed tool call:", 1)[0])
    reference = bundle["assignment"]["source_references"]["objective"]
    assert "objective" not in bundle["assignment"]
    assert bundle["occasion"]["sources"][reference["occasion_source_index"]][reference["field"]] == text
    assert prompt.count(text) == 1
    assert bundle["assignment"]["author"] == {"kind": "unknown"}
