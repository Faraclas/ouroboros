"""Explicit sampling fidelity, optional hints and hypothetical rejection grammars.

All routing/pricing/HTTP is synthetic; real SDK/accounting/codec owners execute.
These checks establish local carriage, never CAPI acceptance or applied sampling.
"""
import copy
import json
from pathlib import Path

import openai
import pytest

from ouroboros import copilot_responses as codec, net_transport as nt
from ouroboros.llm import LLMClient
from ouroboros.llm_routing import resolve_temperature
from ouroboros.observability import read_blob_ref
from tests.test_copilot_option_fidelity import option_wire  # noqa: F401


@pytest.mark.parametrize("value", [None, 0, .7, .2])
def test_codec_copies_canonical_temperature_without_inventing_authorship(value):
    payload = dict(model="gpt-6.1-sol", messages=[], temperature=value)
    before = copy.deepcopy(payload)
    body = codec._copilot_chat_to_responses_payload(payload)
    assert body.get("temperature") == value
    assert ("temperature" in body) == (value is not None)
    assert "default_temperature" not in body and payload == before


@pytest.mark.parametrize("provider,host,model,defer", [
    ("openai-compatible", "https://api.business.githubcopilot.com", "gpt-6.1-sol", True),
    ("openai-compatible", "https://api.githubcopilot.com/v1", "mai-code-future", True),
    ("openai-compatible", "https://api.business.githubcopilot.com", "claude-opus-5.5", False),
    ("openai-compatible", "https://provider.test/v1", "gpt-6.1-sol", False),
    ("openai-compatible", "https://githubcopilot.com.provider.test", "gpt-6.1-sol", False),
    ("openai", "https://api.openai.com/v1", "future-model", False),
    ("anthropic", "https://api.anthropic.com/v1", "future-model", False),
    ("claudexor", "http://127.0.0.1", "future-model", True),
])
@pytest.mark.parametrize("explicit", [None, 0, .7])
def test_route_resolves_only_hints_not_explicit_intent(provider, host, model, defer, explicit):
    target = dict(provider=provider, base_url=host, resolved_model=model)
    before = copy.deepcopy(target)
    expected = explicit if explicit is not None else None if defer else .2
    assert resolve_temperature(target, explicit, .2) == expected
    assert target == before
    request = nt._openai_httpx_module().Request("POST", host.rstrip("/") + "/chat/completions",
                                               json=dict(model=model, messages=[]))
    translated = codec._prepare_request(request, nt._openai_httpx_module()) is not None
    if provider != "claudexor":
        assert translated == defer


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_named_temperature_rejection_is_bounded_disclosed_and_not_learned(option_wire, asynchronous, stream):
    invoke, sent, state, root, task_id = option_wire
    state["error"] = lambda request: (dict(type="invalid_request_error", code="unsupported_parameter",
        param="temperature", message="Unsupported parameter: temperature")
        if "temperature" in request["body"] else None)
    for _ in range(2):  # A previous translated success cannot teach a Chat drop.
        start = len(sent)
        message, usage = invoke(asynchronous=asynchronous, stream=stream, temperature=.7, default_temperature=.2)
        assert json.loads(message["content"]) == {"ok": True}
        assert len(sent) == start + 2
        assert sent[start]["body"]["temperature"] == .7
        assert "temperature" not in sent[start + 1]["body"]
        fact = usage["request_wire"]
        assert fact["candidate_boundary"] == "sdk_input"
        assert fact["compatibility_learning"] == "not_performed_translated_surface"
        assert fact["observed_http"]["temperature"] is None
        assert any("temperature" in item["action"].get("fields", []) for item in fact["applied_actions"])
        assert len(usage["ledger_attempt_ids"]) == 2
    manifests = list((root / "observability" / "calls" / task_id).glob("*_http_*.json"))
    retained = [read_blob_ref(root, json.loads(p.read_text(encoding="utf-8"))["full_payload_ref"]) for p in manifests]
    requests = [item for item in retained if item["kind"] == "request_hook"]
    assert len(requests) == 4
    assert sorted(str(item["request_body"].get("temperature")) for item in requests) == ["0.7", "0.7", "None", "None"]
    assert not list(Path(root).glob("state/request_wire_compatibility.json"))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_value_named_rejection_is_not_silently_removed(option_wire, asynchronous, stream):
    invoke, sent, state, _, _ = option_wire
    state["error"] = dict(type="invalid_request_error", code="unsupported_value", param="temperature",
                          message="Unsupported value: 'temperature' does not support 0.7 with this model")
    with pytest.raises(openai.BadRequestError):
        invoke(asynchronous=asynchronous, stream=stream, temperature=.7)
    assert len(sent) == 1 and sent[0]["body"]["temperature"] == .7


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("model,host", [
    ("claude-opus-5.5", "https://api.business.githubcopilot.com"),
    ("future-model", "https://provider.test/v1"),
])
def test_untranslated_public_hint_remains_a_hint_value(option_wire, monkeypatch, asynchronous, model, host):
    invoke, _, _, _, _ = option_wire
    from ouroboros import request_wire_receipts, request_wire_recovery
    # This control deliberately leaves the translated surface, so restore its
    # legitimate semantic observer instead of the fixture's translated-only trap.
    monkeypatch.setattr(request_wire_recovery, "observe_wire_semantics", request_wire_receipts.observe_wire_semantics)
    target = dict(provider="openai-compatible", resolved_model=model,
                  usage_model="openai-compatible/" + model, api_key="fixture", base_url=host,
                  supports_openrouter_extensions=False)
    monkeypatch.setattr(LLMClient, "_resolve_remote_target", lambda self, model: copy.deepcopy(target))
    hm = nt._openai_httpx_module()
    sent = []
    def handle(self, request):
        sent.append(json.loads(request.read()))
        assert request.url.path.endswith("/chat/completions")
        return hm.Response(200, json=dict(id="chat-fixture", model=model,
            choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content="OK"))],
            usage=dict(prompt_tokens=5, completion_tokens=1, total_tokens=6)))
    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    message, _ = invoke(asynchronous=asynchronous, default_temperature=.2)
    assert message["content"] == "OK" and len(sent) == 1
    assert sent[0]["temperature"] == .2 and "default_temperature" not in sent[0]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_hint_resolution_is_invocation_local_not_a_mutated_caller_default(option_wire, monkeypatch, asynchronous):
    invoke, sent, _, _, _ = option_wire
    original = resolve_temperature
    resolved = []
    def record(target, explicit, hint):
        resolved.append((explicit, hint, original(target, explicit, hint)))
        return original(target, explicit, hint)
    monkeypatch.setattr("ouroboros.llm.resolve_temperature", record)
    options = dict(default_temperature=.2)
    invoke(asynchronous=asynchronous, **options)
    invoke(asynchronous=asynchronous, temperature=0, **options)
    invoke(asynchronous=asynchronous, **options)
    assert resolved == [(None, .2, None), (0, .2, 0), (None, .2, None)]
    assert [row["body"].get("temperature") for row in sent] == [None, 0, None]
    assert options == dict(default_temperature=.2)
