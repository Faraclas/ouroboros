"""Structured option refusals through real compatible retry drivers."""
import asyncio
import copy

import pytest

from ouroboros.llm import LLMClient
from ouroboros.request_wire_recovery import request_wire_call_scope
from ouroboros.usage_accounting import UsageScope, usage_scope
from tests.test_effort_compatibility import _error, _exception
from tests.test_request_wire_recovery_phase2b import _Response, _store, _target
from tests.test_request_wire_recovery_phase2b import evidence_root as _evidence_root

evidence_root = _evidence_root


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("body_error", [False, True])
@pytest.mark.parametrize("effort", [None, "high", "drop-first"])
@pytest.mark.parametrize("error,dropped", [
    pytest.param(_error("Unsupported parameter: top_p. Use temperature instead.", param="top_p"),
                 "top_p", id="alternative-is-not-rejected"),
    pytest.param(_error("Unsupported parameter: unrelated_option. Use temperature instead.", param="unrelated_option"),
                 None, id="foreign-param"),
    pytest.param(_error("temperature not supported", param="temperature", enum=[1.0], value=0.7),
                 None, id="structured-enum"),
    pytest.param(_error("temperature not supported", param="temperature", code="invalid_value"),
                 None, id="structured-value-code"),
    pytest.param(_error("temperature not supported", param="temperature", value=0.7),
                 None, id="structured-scalar"),
    pytest.param(_error("Unsupported parameter: temperature", param="temperature", code="insufficient_quota"),
                 None, id="noncompatibility-code"),
    pytest.param(_error("Unsupported parameter: temperature", param="temperature"),
                 "temperature", id="named-omission-survives"),
    pytest.param(_error("response_format is unsupported", param="response_format"),
                 "response_format", id="optional-format-survives"),
])
def test_structured_rejection_owns_optional_recovery(
    evidence_root, tmp_path, asynchronous, body_error, effort, error, dropped,
):
    target = _target("openai-compatible")
    source = {"model": target["resolved_model"], "messages": [{"role": "user", "content": "inert"}],
              "max_tokens": 32, "temperature": 0.7, "top_p": 0.9,
              "response_format": {"type": "json_object"}}
    if effort is not None:
        source["reasoning_effort"] = "high"
    sent = []
    initial = int(effort == "drop-first")

    def send(**candidate):
        sent.append(copy.deepcopy(candidate))
        rejection = (_error("Unsupported parameter: reasoning_effort", param="reasoning_effort")
                     if initial and len(sent) == 1 else error if len(sent) == initial + 1 else None)
        if rejection:
            if body_error:
                return _Response({"error": rejection, "choices": [], "usage": {}})
            raise _exception(rejection)
        return _Response()

    async def send_async(**candidate):
        return send(**candidate)

    client = LLMClient(api_key="unused")

    def normalize(response):
        body = response.model_dump()
        if "error" not in body:
            message, usage = client._normalize_remote_response(body, target, skip_cost_fetch=True)
            assert message["content"] == "ok"
            if effort is None:
                assert not usage.get("request_wire")
        return response

    async def call_async():
        return normalize(await client._create_chat_completion_with_retries_async(send_async, source, target))

    def call():
        if asynchronous:
            return asyncio.run(call_async())
        return normalize(client._create_chat_completion_with_retries(send, source, target))

    with request_wire_call_scope(), usage_scope(UsageScope(drive_root=tmp_path / "ledger", task_id="binding")):
        if dropped is None and not body_error:
            with pytest.raises(type(_exception(error))):
                call()
        else:
            response = call()
            if dropped is None:
                assert response.model_dump()["error"] == error
    assert len(sent) == initial + 1 + int(dropped is not None)
    if initial:
        assert "reasoning_effort" not in sent[1]
    if dropped:
        before, after = sent[-2:]
        assert after == {key: value for key, value in before.items() if key != dropped}
    if effort is None or dropped is None:
        assert _store(evidence_root) is None
    assert source["temperature"] == 0.7 and source["top_p"] == 0.9


@pytest.mark.parametrize("body_error", [False, True])
def test_registered_decline_cannot_escape_composition_bound(evidence_root, monkeypatch, body_error):
    from ouroboros import request_wire_recovery as recovery

    monkeypatch.setattr(recovery, "_MAX_COMPOSED_ACTIONS", 1)
    target = _target("openai-compatible")
    source = {"model": target["resolved_model"], "messages": [], "max_tokens": 32,
              "reasoning_effort": "high", "temperature": 0.7}
    failure = _error("Unsupported parameter: reasoning_effort", param="reasoning_effort")
    optional = _error("Unsupported parameter: temperature", param="temperature")
    with request_wire_call_scope():
        sent = recovery.prepare_wire_payload_for_send(target, source, api_surface="chat.completions")
        dropped = recovery.plan_next_wire_retry(sent, error=failure if body_error else _exception(failure), body_error=body_error)
        assert dropped is not None and "reasoning_effort" not in dropped
        assert recovery.plan_next_wire_retry(dropped, error=optional if body_error else _exception(optional), body_error=body_error) is None
        assert recovery.current_wire_candidate().physical_payload()["temperature"] == 0.7


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("effort", ["", "high"])
@pytest.mark.parametrize("param,fields,dropped", [
    ("top_p", {}, None),
    ("temperature", {"enum": [1.0], "value": 0.7}, None),
    ("temperature", {}, "temperature"),
])
def test_native_anthropic_uses_the_same_rejection_ownership(
    evidence_root, tmp_path, monkeypatch, asynchronous, effort, param, fields, dropped,
):
    import json
    import requests

    target = _target("anthropic", "claude-future")
    error = {"type": "invalid_request_error", "param": param,
             "message": f"Unsupported parameter: {param}. Use temperature instead.", **fields}
    sent = []

    def post(url, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        response = requests.Response()
        response.url = url
        response.status_code = 400 if len(sent) == 1 else 200
        response._content = json.dumps({"error": error} if len(sent) == 1 else {
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {},
        }).encode("utf-8")
        return response

    monkeypatch.setattr(requests, "post", post)
    client = LLMClient(api_key="unused")
    args = (target, [{"role": "user", "content": "inert"}], None, effort, 32, "auto", 0.7)
    with usage_scope(UsageScope(drive_root=tmp_path / "ledger", task_id="native-binding")):
        if dropped is None:
            with pytest.raises(requests.HTTPError):
                asyncio.run(client._chat_anthropic_async(*args)) if asynchronous else client._chat_anthropic(*args)
        else:
            message, _ = asyncio.run(client._chat_anthropic_async(*args)) if asynchronous else client._chat_anthropic(*args)
            assert message["content"] == "ok"
    assert len(sent) == 1 + int(dropped is not None)
    assert sent[0]["temperature"] == 0.7
    if dropped:
        assert sent[1] == {key: value for key, value in sent[0].items() if key != dropped}


@pytest.mark.parametrize("body_error", [False, True])
@pytest.mark.parametrize("error,extra,dropped", [
    (_error("Unsupported parameters: temperature, top_p"), {}, {"temperature", "top_p"}),
    (_error("Unsupported parameter: stream", param="stream"),
     {"stream": True, "stream_options": {"include_usage": True}}, {"stream", "stream_options"}),
    (_error("Unsupported parameter: stream_options", param="stream_options"),
     {"stream": True, "stream_options": {"include_usage": True}}, {"stream_options"}),
])
def test_carrierless_prose_and_stream_pairing_survive(body_error, error, extra, dropped):
    from ouroboros.request_wire_recovery import plan_nonlearning_optional_retry

    source = {"model": "future", "temperature": 0.7, "top_p": 0.9, **extra}
    failure = {"status": error["status"], "error": {k: v for k, v in error.items() if k != "status"}}
    assert plan_nonlearning_optional_retry(source, error=failure if body_error else _exception(error), body_error=body_error) == {
        k: v for k, v in source.items() if k not in dropped}
