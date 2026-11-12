"""Structured error binding owns cache-hint recovery, including explanatory prose."""
from __future__ import annotations

import asyncio
import copy
import json
import socket

import pytest

from tests.test_compatible_cache_hints import (
    _Refusal, _Response, _learned, _markers, _payload, _target,
)
import ouroboros.request_wire_contract as wire
from ouroboros.llm import LLMClient
from ouroboros.request_wire_recovery import (
    plan_wire_retry_from_body_error, plan_wire_retry_from_exception,
    prepare_wire_payload_for_send, request_wire_call_scope,
)
from ouroboros.usage_accounting import UsageScope, usage_scope


@pytest.fixture(autouse=True)
def isolated_wire(tmp_path, monkeypatch):
    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setenv("OUROBOROS_PROMPT_CACHE_TTL", "1h")
    def blocked(*args, **kwargs):
        raise AssertionError("This regression cannot contact a provider")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("body_error", [False, True], ids=["exception", "body"])
@pytest.mark.parametrize("message", [
    "Unsupported parameter: top_p. Use cache_control instead.",
    "Unsupported parameter: top_p. tools.1.cache_control is supported.",
])
def test_unrelated_sampling_rejection_keeps_markers_through_driver(
    tmp_path, asynchronous, body_error, message,
):
    target = _target(model="binding-driver")
    payload = _payload(target)[0]
    payload["top_p"] = 0.7
    original = copy.deepcopy(payload)
    error = {"error": {"param": "top_p", "message": message,
                       "type": "invalid_request_error", "code": 400}}
    sent = []
    class Response(_Response):
        def model_dump(self):
            return copy.deepcopy(error) if "top_p" in sent[-1] and body_error else super().model_dump()
    def create(**candidate):
        candidate.pop("timeout", None)
        sent.append(copy.deepcopy(candidate))
        if "top_p" in candidate and not body_error:
            raise _Refusal(error)
        return Response()
    async def create_async(**candidate):
        return create(**candidate)
    client = LLMClient(api_key="unused")
    def normalize(response):
        _, usage = client._normalize_remote_response(response.model_dump(), target, skip_cost_fetch=True,
                                                     prompt_cache_ttl="1h")
        return response, usage
    async def invoke_async():
        response = await client._create_chat_completion_with_retries_async(create_async, payload, target)
        return normalize(response)  # Consume task-local disclosures in their producing context.
    with request_wire_call_scope(), usage_scope(UsageScope(drive_root=tmp_path / "driver", task_id="binding-driver")):
        response, usage = (asyncio.run(invoke_async()) if asynchronous else
                           normalize(client._create_chat_completion_with_retries(create, payload, target)))
    assert len(sent) == 2
    assert len(_markers(sent[0])) == len(_markers(sent[1])) == 4
    expected = copy.deepcopy(sent[0]); expected.pop("top_p")
    assert sent[1] == expected
    assert payload == original
    assert response.model_dump()["choices"][0]["message"]["content"] == "ok"
    actions = usage["request_wire"]["applied_actions"]
    assert [a["action"]["fields"] for a in actions] == [["top_p"]]
    assert _learned(tmp_path) == ["top_p"]


@pytest.mark.parametrize("body_error", [False, True], ids=["exception", "body"])
@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
@pytest.mark.parametrize("param", ["response_format.type", "temperature", "unknown_option"])
def test_explicit_schema_binding_is_not_a_fieldless_cache_refusal(provider, param, body_error):
    target = _target(provider=provider, model="binding-planner")
    payload = {"model": target["resolved_model"], "max_tokens": 512,
               "messages": [{"role": "user", "content": [{"type": "text", "text": "task",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}],
               "response_format": {"type": "json_object"}, "temperature": 0.7}
    if provider == "anthropic":
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": "high"}
    else:
        payload["reasoning_effort"] = "high"
    before = copy.deepcopy(payload)
    error = {"status": 400, "error": {"param": param, "message": "Input should be a valid string"}}
    with request_wire_call_scope():
        prepare_wire_payload_for_send(target, payload, api_surface="messages" if provider == "anthropic" else "chat.completions")
        repaired = (plan_wire_retry_from_body_error(error) if body_error
                    else plan_wire_retry_from_exception(_Refusal(error)))
    assert repaired is None
    assert payload == before


@pytest.mark.parametrize("body_error", [False, True])
@pytest.mark.parametrize("param,remaining", [
    ("cache_control", 0),
    ("messages.0.content.0.cache_control.ttl", 4),
    ("tools.1.cache_control", 3),
])
def test_named_hint_scope_survives_binding_gate(param, remaining, body_error):
    target = _target(model="named-scope")
    payload = _payload(target)[0]
    error = {"status": 400, "error": {"param": param,
        "message": f"Unsupported parameter: {param}"}}
    with request_wire_call_scope():
        prepare_wire_payload_for_send(target, payload, api_surface="chat.completions")
        repaired = (plan_wire_retry_from_body_error(error) if body_error
                    else plan_wire_retry_from_exception(_Refusal(error)))
    assert repaired is not None
    assert len(_markers(repaired)) == remaining
    if param.endswith(".ttl"):
        assert _markers(repaired) == [{"type": "ephemeral"}] * 4
    elif param == "cache_control":
        assert not _markers(repaired)
    else:
        assert all("cache_control" not in t for t in repaired["tools"])
        assert all(m["ttl"] == "1h" for m in _markers(repaired))


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_genuinely_unbound_schema_refusal_still_omits_markers(provider):
    target = _target(provider=provider, model="unbound")
    payload = {"model": "unbound", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "task", "cache_control": {"type": "ephemeral"}}]}], "max_tokens": 512}
    if provider == "anthropic":
        payload.update(thinking={"type": "adaptive"}, output_config={"effort": "high"})
    else:
        payload["reasoning_effort"] = "high"
    with request_wire_call_scope():
        prepare_wire_payload_for_send(target, payload, api_surface="messages" if provider == "anthropic" else "chat.completions")
        repaired = plan_wire_retry_from_exception(_Refusal({"error": {"message": "Extra inputs are not permitted"}}))
    assert repaired is not None and not _markers(repaired)


@pytest.mark.parametrize("param,scope", [
    ("messages[0].content", "all"), ("messages.5.content", "all"),
    ("system.0", "all"), ("tools", "tools"),
    ("messages.0.content.0.cache_control", "ttl"),
])
def test_marker_bearing_container_binding_preserves_schema_recovery(param, scope):
    target = _target(model="container")
    payload = _payload(target)[0]
    message = ("cache_control.ttl: Extra inputs are not permitted" if scope == "ttl" else
               "tools.1.cache_control: Extra inputs are not permitted" if scope == "tools" else
               "Input should be a valid string")
    with request_wire_call_scope():
        prepare_wire_payload_for_send(target, payload, api_surface="chat.completions")
        repaired = plan_wire_retry_from_exception(_Refusal({"error": {"param": param, "message": message}}))
    assert repaired is not None
    assert len(_markers(repaired)) == {"all": 0, "tools": 3, "ttl": 4}[scope]
    if scope == "ttl":
        assert _markers(repaired) == [{"type": "ephemeral"}] * 4


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("param,allowed", [("temperature", False), ("messages.0.content", True), (None, True)])
def test_native_anthropic_marked_requests_keep_binding_and_container_fallback(
    tmp_path, monkeypatch, asynchronous, param, allowed,
):
    import requests
    target = _target(provider="anthropic", model="claude-future")
    sent = []
    def post(url, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        response = requests.Response(); response.url = url
        response.status_code = 400 if len(sent) == 1 else 200
        response._content = json.dumps({"error": {"type": "invalid_request_error", "param": param,
            "message": "Input should be a valid string; cache_control is not supported"}}
            if len(sent) == 1 else {"content": [{"type": "text", "text": "ok"}],
                                  "stop_reason": "end_turn", "usage": {}}).encode("utf-8")
        return response
    monkeypatch.setattr(requests, "post", post)
    client = LLMClient(api_key="unused")
    args = (target, [{"role": "system", "content": [{"type": "text", "text": "stable",
             "cache_control": {"type": "ephemeral"}}]}, {"role": "user", "content": "inert"}],
            None, "high", 512, "auto", 0.7)
    with usage_scope(UsageScope(drive_root=tmp_path / "native", task_id="native-cache-binding")):
        if allowed:
            message, _ = asyncio.run(client._chat_anthropic_async(*args)) if asynchronous else client._chat_anthropic(*args)
            assert message["content"] == "ok"
        else:
            with pytest.raises(requests.HTTPError):
                asyncio.run(client._chat_anthropic_async(*args)) if asynchronous else client._chat_anthropic(*args)
    assert len(sent) == 1 + int(allowed)
    assert _markers(sent[0])
    if allowed:
        assert not _markers(sent[1]) and sent[1]["temperature"] == 0.7
