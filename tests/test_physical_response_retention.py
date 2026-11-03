"""Complete materialized provider bodies survive before lane normalization."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from dataclasses import replace

import pytest
from openai.types.chat import ChatCompletion

from ouroboros import llm_attempt, llm_observability
from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.observability import read_call_payload
from tests.test_physical_candidate_capture import (
    _rows, _scope, _target, data_root as data_root,
)


pytestmark = pytest.mark.serial


def _body():
    return {
        "id": "chatcmpl-retained", "model": "gpt-5.2", "created": 1,
        "object": "chat.completion", "service_tier": "priority",
        "choices": [
            {"index": 0, "finish_reason": "length", "logprobs": None,
             "message": {"role": "assistant", "content": "first 🐍\r\n", "refusal": None}},
            {"index": 1, "finish_reason": "stop", "logprobs": None,
             "message": {"role": "assistant", "content": "second", "refusal": "native refusal"}},
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11,
                  "completion_tokens_details": {"reasoning_tokens": 1}},
        "provider_extension": {"terminal": True, "opaque": "unmodified"},
    }


def _retained(root, capture, task_id):
    manifest, payload, _ref = read_call_payload(
        root, task_id=task_id, call_id=f"physical_{capture.attempt_id}_response",
    )
    assert manifest["call_type"] == "physical_response"
    assert manifest["attempt_id"] == capture.attempt_id
    assert manifest["full_payload_redacted"] is False
    assert payload["physical_attempt_capture"]["candidate_manifest_ref"] == capture.candidate_manifest_ref
    assert payload["physical_attempt_capture"]["state"] == capture.state
    return manifest, payload["response"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_openai_sdk_full_response_retained_before_caller_normalization(data_root, asynchronous):
    response = ChatCompletion.model_validate(_body())
    client, sent = LLMClient(api_key="unused"), []
    messages = [{"role": "user", "content": "once"}]
    original = copy.deepcopy(messages)
    candidate = {"model": "gpt-5.2", "messages": messages, "max_tokens": 64, "n": 2}

    def create(**kwargs):
        sent.append(kwargs)
        return response

    async def create_async(**kwargs):
        return create(**kwargs)

    async def run():
        value = await client._create_chat_completion_with_retries_async(create_async, candidate, _target())
        return value, ua.last_physical_attempt_capture()

    with ua.usage_scope(_scope(data_root)), ua.physical_attempt_limit(1):
        if asynchronous:
            value, capture = asyncio.run(run())
        else:
            value = client._create_chat_completion_with_retries(create, candidate, _target())
            capture = ua.last_physical_attempt_capture()
    assert value is response and len(sent) == 1
    assert messages == original
    assert sent[0]["messages"] == original
    manifest, body = _retained(data_root, capture, "task-physical")
    assert manifest["control_reason"] == "received"
    assert body == response.model_dump()
    assert [choice["finish_reason"] for choice in body["choices"]] == ["length", "stop"]
    assert body["provider_extension"] == _body()["provider_extension"]
    assert _rows(data_root)[-1]["attempt_id"] == capture.attempt_id
    # A caller/normalizer failure cannot remove the already retained provider body.
    with pytest.raises(RuntimeError, match="caller normalization failed"):
        raise RuntimeError("caller normalization failed")
    assert _retained(data_root, capture, "task-physical")[1] == body


def test_native_anthropic_requests_body_is_retained_before_normalizer(data_root, monkeypatch):
    import requests

    body = {
        "id": "msg-retained", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "thinking", "thinking": "native reasoning", "signature": "signed"},
                    {"type": "text", "text": "paid answer 🐍\r\n"}],
        "stop_reason": "max_tokens", "stop_sequence": "END",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "provider_extension": {"full": True},
    }
    response = requests.Response()
    response.status_code, response.reason = 200, "OK"
    response.url = "https://anthropic.invalid/v1/messages"
    response._content = json.dumps(body, ensure_ascii=False).encode()
    sends = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sends.append(k) or response)
    client = LLMClient(api_key="unused")
    target = {"provider": "anthropic", "usage_model": "anthropic/claude-test",
              "resolved_model": "claude-test", "base_url": "https://anthropic.invalid/v1", "api_key": "unused"}

    def normalize(*args, **kwargs):
        capture = ua.last_physical_attempt_capture()
        assert _retained(data_root, capture, "task-anthropic")[1] == body
        raise RuntimeError("normalizer failed")

    monkeypatch.setattr(client, "_normalize_anthropic_response", normalize)
    with ua.usage_scope(_scope(data_root, "task-anthropic")), ua.physical_attempt_limit(1):
        with pytest.raises(RuntimeError, match="normalizer failed"):
            client._chat_anthropic(target, [{"role": "user", "content": "once"}], None, "none", 64, "auto")
    assert len(sends) == 1
    capture = ua.last_physical_attempt_capture()
    assert _retained(data_root, capture, "task-anthropic")[1] == body


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failure", ["persist", "accounting"])
def test_failures_do_not_discard_response_or_resend(data_root, monkeypatch, caplog, asynchronous, failure):
    response = _body()  # Materialized decoded stream bodies need no SDK wrapper.
    request = ua.AttemptRequest(model="openai/gpt-5.2", provider="openai", drive_root=data_root,
                                task_id="task-diagnostic", reservation_usd=0.0, allow_live_fetch=False)
    sends = []
    if failure == "persist":
        def fail(*args, **kwargs):
            raise OSError("private response store unavailable")
        monkeypatch.setattr(llm_observability, "persist_call", fail)
    else:
        def fail(*args, **kwargs):
            raise OSError("settlement unavailable")
        monkeypatch.setattr(ua, "settle_attempt", fail)

    def send():
        sends.append(True)
        return response

    async def send_async():
        return send()

    async def run():
        value = await llm_attempt._execute_candidate_async(request, send_async, None)
        return value, ua.last_physical_attempt_capture()

    with ua.physical_attempt_limit(1):
        if asynchronous:
            value, capture = asyncio.run(run())
        else:
            value = llm_attempt._execute_candidate(request, send, None)
            capture = ua.last_physical_attempt_capture()
    assert value is response and len(sends) == 1
    assert capture.state == ("settled" if failure == "persist" else "unresolved")
    if failure == "persist":
        assert "Physical response retention gap (OSError)" in caplog.text
    else:
        assert _retained(data_root, capture, "task-diagnostic")[1] == response


def test_existing_cancel_retainer_handles_requests_json(data_root):
    import requests

    response = requests.Response()
    response._content = json.dumps(_body()).encode()
    request = ua.AttemptRequest(model="openai/gpt-5.2", provider="openai", drive_root=data_root,
                                task_id="task-cancel-json", reservation_usd=0.0, allow_live_fetch=False)
    ua.execute_physical_attempt(request, lambda: response)
    capture = ua.last_physical_attempt_capture()
    ref = llm_observability.retain_cancelled_response(request, response, capture)
    assert ref["manifest_ref"]
    assert _retained(data_root, capture, "task-cancel-json")[1] == _body()


def test_missing_or_mismatched_capture_root_is_diagnostic_not_misattributed(data_root, monkeypatch, caplog):
    request = ua.AttemptRequest(model="openai/gpt-5.2", provider="openai", drive_root=data_root,
                                task_id="task-capture", reservation_usd=0.0, allow_live_fetch=False)
    ua.execute_physical_attempt(request, lambda: _body())
    capture = ua.last_physical_attempt_capture()
    writes = []
    monkeypatch.setattr(llm_observability, "persist_call", lambda *a, **k: writes.append(k))
    assert llm_observability.retain_received_response(replace(request, model="other"), _body()) == {}
    ua.adopt_physical_attempt_capture(replace(capture, attempt_id="different"))
    assert llm_observability.retain_received_response(request, _body()) == {}
    assert not writes
    assert "Physical response retention gap (ValueError)" in caplog.text


def test_async_cancel_during_retention_joins_write_and_preserves_response(data_root, monkeypatch):
    response = _body()
    entered, release = threading.Event(), threading.Event()
    original = llm_observability._retain_physical_response
    sends, retained = [], []

    def retain(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        result = original(*args, **kwargs)
        retained.append(result)
        return result

    monkeypatch.setattr(llm_observability, "_retain_physical_response", retain)
    request = ua.AttemptRequest(model="openai/gpt-5.2", provider="openai", drive_root=data_root,
                                task_id="task-cancel-write", reservation_usd=0.0, allow_live_fetch=False)

    async def send():
        sends.append(True)
        return response

    async def run():
        operation = asyncio.create_task(llm_attempt._execute_candidate_async(request, send, None))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            operation.cancel()
            await asyncio.sleep(.01)
            operation.cancel()
            assert not operation.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await operation
            assert caught.value.response is response
            assert caught.value.response_manifest_ref
            return caught.value.physical_attempt_capture
        finally:
            release.set()
            if not operation.done():
                await asyncio.gather(operation, return_exceptions=True)

    with ua.physical_attempt_limit(1):
        capture = asyncio.run(run())
    assert len(sends) == 1 and retained
    assert _retained(data_root, capture, "task-cancel-write")[1] == response
