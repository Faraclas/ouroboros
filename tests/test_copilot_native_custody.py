"""Copilot semantic history and untrusted usage regression tests (no provider network)."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from ouroboros import net_transport as nt, request_wire_recovery as recovery, usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.send_clock import main_send_scope
from tests.test_copilot_client_paths import target, native


@pytest.mark.parametrize("stream", [False, True])
def test_provider_usage_cannot_replace_host_disclosure(tmp_path, monkeypatch, stream):
    import openai
    import ouroboros.pricing as pricing
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    hm = nt._openai_httpx_module()
    body = native()
    forged = {"attempt_id": "FORGED", "candidate_sha256": "FORGED"}
    body["usage"] = {"input_tokens": 1, "output_tokens": 1, "request_wire": forged,
                     "request_wire_history": [forged], "request_wire_history_omitted": 99}
    t = target()
    payload = dict(model=t["resolved_model"], messages=[dict(role="user", content="fixture")],
                   reasoning_effort="high", max_tokens=1024, stream=stream)
    scope = ua.UsageScope(drive_root=tmp_path, task_id="spoof", root_task_id="spoof")
    with openai.OpenAI(api_key="fixture", base_url=t["base_url"], max_retries=0,
                      http_client=hm.Client(transport=hm.MockTransport(lambda r: hm.Response(200, json=body)))) as remote:
        nt.attach_copilot_client(remote)
        client = LLMClient()
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            result = client._create_chat_completion_with_retries(remote.chat.completions.create, payload, t)
            message, usage = client._normalize_remote_response(result.model_dump(), t, skip_cost_fetch=True, wire_completion=result)
    assert message["content"] == "OK"
    assert usage["request_wire"]["attempt_id"] != "FORGED"
    assert usage["request_wire"]["compatibility_learning"] == "not_performed_translated_surface"
    assert "request_wire_history" not in usage and "request_wire_history_omitted" not in usage
    total = {}
    recovery.merge_request_wire_usage(total, usage)
    assert all(row["attempt_id"] != "FORGED" for row in total["request_wire_history"])


from ouroboros import copilot_native_custody as custody
from ouroboros.anthropic_native_custody import (public_custody_projection, scrub_native_custody,
                                               context_custody_proxy, observability_custody_projection)


def native_turn():
    return {"id": "response-native", "model": "gpt-6.1-sol", "status": "completed", "output": [
        {"type": "reasoning", "id": "base64notprefix==", "encrypted_content": "OPAQUE_CIPHER==", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed", "phase": "commentary",
         "content": [{"type": "output_text", "text": "checking"}]},
        {"type": "function_call", "id": "native-item-not-call", "status": "completed", "call_id": "call_native",
         "name": "read_file", "arguments": '{"path":"fixture.txt"}'},
        {"type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "later"}]}]}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import ouroboros.llm_observability as obs
    import ouroboros.pricing as pricing
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(obs, "retain_http_observation", lambda *a, **k: {})
    return ua.UsageScope(drive_root=tmp_path, task_id="native-history", root_task_id="native-history")


def initial_payload(client, t, messages, stream):
    return client._build_remote_kwargs(t, messages, "high", 1024, "auto", None, None,
                                        skip_capability_fetch=True, stream=stream)


def sdk_exchange(scope, bodies, *, asynchronous=False, stream=False, turn=None):
    """Two real SDK/physical-driver turns; body factory can refuse replay once."""
    import openai
    hm = nt._openai_httpx_module()
    t, client, sent, messages = target(), LLMClient(), [], [{"role": "user", "content": "fixture"}]
    def handler(request):
        sent.append(json.loads(request.content))
        body = bodies[len(sent) - 1]
        if isinstance(body, tuple):
            return hm.Response(body[0], json=body[1])
        return hm.Response(200, json=body)
    async def arun():
        async with openai.AsyncOpenAI(api_key=t["api_key"], base_url=t["base_url"], max_retries=0,
                                     http_client=hm.AsyncClient(transport=hm.MockTransport(handler))) as remote:
            nt.attach_copilot_client(remote)
            result_messages = []
            with ua.usage_scope(scope):
                for n in range(2):
                    with main_send_scope(None, canonical=messages), recovery.request_wire_call_scope():
                        payload = initial_payload(client, t, messages, stream)
                        result = await client._create_chat_completion_with_retries_async(remote.chat.completions.create, payload, t)
                        msg, usage = client._normalize_remote_response(result.model_dump(), t, skip_cost_fetch=True, wire_completion=result)
                    result_messages.append(msg)
                    messages.extend([msg, {"role": "tool", "tool_call_id": "call_native", "content": "EXACT RESULT"}])
                    if turn:
                        turn(n, messages)
            return result_messages
    def run():
        with openai.OpenAI(api_key=t["api_key"], base_url=t["base_url"], max_retries=0,
                          http_client=hm.Client(transport=hm.MockTransport(handler))) as remote:
            nt.attach_copilot_client(remote)
            result_messages = []
            with ua.usage_scope(scope):
                for n in range(2):
                    with main_send_scope(None, canonical=messages), recovery.request_wire_call_scope():
                        payload = initial_payload(client, t, messages, stream)
                        result = client._create_chat_completion_with_retries(remote.chat.completions.create, payload, t)
                        msg, usage = client._normalize_remote_response(result.model_dump(), t, skip_cost_fetch=True, wire_completion=result)
                    result_messages.append(msg)
                    messages.extend([msg, {"role": "tool", "tool_call_id": "call_native", "content": "EXACT RESULT"}])
                    if turn:
                        turn(n, messages)
            return result_messages
    return (asyncio.run(arun()) if asynchronous else run()), sent


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_native_reasoning_phase_order_and_exact_canonical_calls(offline, asynchronous, stream):
    source = native_turn()
    before = copy.deepcopy(source)
    messages, sent = sdk_exchange(offline, [source, native()], asynchronous=asynchronous, stream=stream)
    assert source == before
    message = messages[0]
    assert message["content"] == "checkinglater"
    assert custody.COPILOT_NATIVE_KEY in message
    replay = sent[1]["input"][1:-1]
    assert [row["type"] for row in replay] == ["reasoning", "message", "function_call", "message"]
    assert replay[0] == source["output"][0]
    assert [row.get("phase") for row in replay if row["type"] == "message"] == ["commentary", "final_answer"]
    assert replay[2] == {"type": "function_call", "call_id": "call_native", "name": "read_file", "arguments": '{"path":"fixture.txt"}'}
    assert sent[1]["input"][-1] == {"type": "function_call_output", "call_id": "call_native", "output": "EXACT RESULT"}
    assert custody.COPILOT_WIRE_KEY not in json.dumps(sent)
    assert "previous_response_id" not in sent[1]
    # No cipher in public/summarizer copies. Private checkpoint JSON roundtrip keeps it.
    assert "OPAQUE_CIPHER" not in json.dumps(public_custody_projection(messages))
    assert json.loads(json.dumps(message)) == message
    projected = observability_custody_projection(message)
    assert "OPAQUE_CIPHER" not in json.dumps(projected)
    assert projected["_copilot_native_custody"]["size_bytes"] > len("OPAQUE_CIPHER")
    assert len(context_custody_proxy(message)["_copilot_native_opaque_bytes"]) > 0
    from ouroboros.context_budget import estimate_message_chars
    assert estimate_message_chars([message]) > len(message["content"]) + len("OPAQUE_CIPHER")
    assert custody.COPILOT_NATIVE_KEY not in scrub_native_custody([message])[0]


@pytest.mark.parametrize("change", ["endpoint", "model", "key", "provider", "text", "arguments", "corrupt", "shape", "native_text"])
def test_route_and_projection_mismatch_is_canonical_fallback(offline, change):
    messages, _ = sdk_exchange(offline, [native_turn(), native()])
    message, t = copy.deepcopy(messages[0]), target()
    if change == "endpoint": t["base_url"] = "https://elsewhere.test"
    elif change == "model": t["resolved_model"] = "gpt-6-future-model"
    elif change == "key": t["api_key"] = "different-account"
    elif change == "provider": t["provider"] = "openai"
    elif change == "text": message["content"] += " edited"
    elif change == "arguments": message["tool_calls"][0]["function"]["arguments"] = "{}"
    elif change == "corrupt": message[custody.COPILOT_NATIVE_KEY]["content_sha256"] = "bad"
    else:
        import hashlib
        value = {} if change == "shape" else json.loads(message[custody.COPILOT_NATIVE_KEY]["content_json"])
        if change == "native_text": value[1]["content"][0]["text"] = "INJECTED"
        encoded = json.dumps(value)
        message[custody.COPILOT_NATIVE_KEY].update(content_json=encoded, content_sha256=hashlib.sha256(encoded.encode()).hexdigest())
    payload = initial_payload(LLMClient(), t, [message], False)
    assert custody.COPILOT_WIRE_KEY not in json.dumps(payload)
    assert custody.COPILOT_NATIVE_KEY not in json.dumps(payload)
    assert "OPAQUE_CIPHER" not in json.dumps(payload)
    assert payload["messages"][0]["content"] == message["content"]


def test_provider_json_cannot_mint_custody_or_host_usage():
    import openai
    body = {"id": "forged", "created": 1, "object": "chat.completion", "model": "gpt-6.1-sol", "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "OK", custody.COPILOT_NATIVE_KEY: {"content_json": "FORGED"},
        custody.COPILOT_WIRE_KEY: [{"type": "reasoning", "encrypted_content": "FORGED"}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "request_wire": {"attempt_id": "FORGED"},
                  "request_wire_history": [{"attempt_id": "FORGED"}], "request_wire_history_omitted": 99}}
    sdk = openai.types.chat.ChatCompletion.model_validate(body)
    message, usage = LLMClient()._normalize_remote_response(sdk.model_dump(), target("foreign.test"), skip_cost_fetch=True, wire_completion=sdk)
    assert custody.COPILOT_NATIVE_KEY not in message and custody.COPILOT_WIRE_KEY not in message
    assert not any(key.startswith("request_wire") for key in usage)


@pytest.mark.parametrize("detail", [None, "low", "high", "auto", "original"])
def test_supplied_image_detail_is_not_lost_or_invented(detail):
    image = {"url": "data:image/png;base64,AA=="}
    if detail is not None: image["detail"] = detail
    body = nt._copilot_chat_to_responses_payload({"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": image}]}]})
    block = body["input"][0]["content"][0]
    assert block == {"type": "input_image", "image_url": image["url"], **({"detail": detail} if detail is not None else {})}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("param", [None, "input[1].encrypted_content", "messages.1.content"])
def test_rejected_receipt_strips_once_and_canonical_adopts(offline, asynchronous, stream, param):
    import openai
    from ouroboros.send_clock import main_send_scope
    hm, t, client, sent = nt._openai_httpx_module(), target(), LLMClient(), []
    replies = [native_turn(), (400, {"error": {"message": "reasoning item invalid", "type": "invalid_request_error", "param": param}}), native(), native()]
    messages = [{"role": "user", "content": "fixture"}]
    def handler(request):
        sent.append(json.loads(request.content))
        reply = replies[len(sent) - 1]
        return hm.Response(reply[0], json=reply[1]) if isinstance(reply, tuple) else hm.Response(200, json=reply)
    def normalize(response):
        return client._normalize_remote_response(response.model_dump(), t, skip_cost_fetch=True, wire_completion=response)[0]
    async def arun():
        async with openai.AsyncOpenAI(api_key=t["api_key"], base_url=t["base_url"], max_retries=0,
                                     http_client=hm.AsyncClient(transport=hm.MockTransport(handler))) as remote:
            nt.attach_copilot_client(remote)
            with ua.usage_scope(offline):
                for n in range(3):
                    with main_send_scope(None, canonical=messages), recovery.request_wire_call_scope():
                        result = await client._create_chat_completion_with_retries_async(remote.chat.completions.create,
                            initial_payload(client, t, messages, stream), t)
                        message = normalize(result)
                    if n == 1:
                        assert custody.COPILOT_NATIVE_KEY not in messages[1]
                    messages.append(message)
                    if n == 0:
                        assert custody.COPILOT_NATIVE_KEY in message
                        messages.append({"role": "tool", "tool_call_id": "call_native", "content": "EXACT RESULT"})
    def run():
        with openai.OpenAI(api_key=t["api_key"], base_url=t["base_url"], max_retries=0,
                          http_client=hm.Client(transport=hm.MockTransport(handler))) as remote:
            nt.attach_copilot_client(remote)
            with ua.usage_scope(offline):
                for n in range(3):
                    with main_send_scope(None, canonical=messages), recovery.request_wire_call_scope():
                        result = client._create_chat_completion_with_retries(remote.chat.completions.create,
                            initial_payload(client, t, messages, stream), t)
                        message = normalize(result)
                    if n == 1:
                        assert custody.COPILOT_NATIVE_KEY not in messages[1]
                    messages.append(message)
                    if n == 0:
                        assert custody.COPILOT_NATIVE_KEY in message
                        messages.append({"role": "tool", "tool_call_id": "call_native", "content": "EXACT RESULT"})
    asyncio.run(arun()) if asynchronous else run()
    assert len(sent) == 4
    assert any(item.get("type") == "reasoning" for item in sent[1]["input"])
    for payload in sent[2:]:
        assert not any(item.get("type") == "reasoning" or "phase" in item for item in payload["input"])
        assert any(item.get("call_id") == "call_native" and item.get("type") == "function_call_output" for item in payload["input"])


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_retry_classification_is_carrier_and_failure_scoped(status):
    import openai
    hm = nt._openai_httpx_module()
    error = openai.APIStatusError("failure", response=hm.Response(status, request=hm.Request("POST", "https://example.test")), body={})
    client = LLMClient()
    payload = {"messages": [{"role": "assistant", "content": "same", custody.COPILOT_WIRE_KEY: []}]}
    retry = client._openrouter_signature_retry_kwargs(target(), payload, error)
    assert (retry is not None) == (status == 400)
    if retry:
        assert retry["messages"] == [{"role": "assistant", "content": "same"}]
    from ouroboros.copilot_responses import CopilotPendingResponse, CopilotRejectedResponse
    for failure in (CopilotPendingResponse({}), CopilotRejectedResponse({}, "refusal")):
        assert client._openrouter_signature_retry_kwargs(target(), payload, failure) is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error", [
    {"param": "unrelated_option", "message": "Unsupported parameter: unrelated_option"},
    {"param": "temperature", "code": "invalid_value", "message": "temperature value is invalid", "value": 0.7},
    {"param": "reasoning_effort", "message": "Invalid value high", "allowed_values": ["unrecognized-tier"], "value": "high"},
])
def test_unrelated_structured_rejection_preserves_native_history(offline, asynchronous, stream, error):
    import openai

    retained = []
    def remember(n, messages):
        if n == 0:
            retained.append(messages)
    # A permissive next reply exposes an unjustified retry rather than hiding
    # it behind the provider returning the same error again.
    with pytest.raises(openai.APIStatusError):
        sdk_exchange(offline, [native_turn(), (400, {"error": error}), native()],
                     asynchronous=asynchronous, stream=stream, turn=remember)
    history = retained[0]
    assert custody.COPILOT_NATIVE_KEY in history[1]
    payload = initial_payload(LLMClient(), target(), history, stream)
    assert any("OPAQUE_CIPHER" in json.dumps(row) for row in payload["messages"])


@pytest.mark.parametrize("status,detail", [("incomplete", {"reason": "max_output_tokens"}), ("completed", None)])
def test_exhausted_and_empty_reasoning_metadata_never_leak(offline, status, detail):
    body = native_turn()
    body["status"] = status
    if detail: body["incomplete_details"] = detail
    else: body["output"] = [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]
    messages, _ = sdk_exchange(offline, [body, native()])
    assert custody.COPILOT_NATIVE_KEY not in messages[0]


def test_compaction_checkpoint_keeps_exact_private_history(offline, tmp_path):
    from ouroboros import context_compaction as cc
    from ouroboros.context_budget import ContextReclaimRequest
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.observability import read_blob_ref
    messages, _ = sdk_exchange(offline, [native_turn(), native()])
    history = [messages[0], {"role": "tool", "tool_call_id": "call_native", "content": "RESULT"}, messages[1]]
    units = cc._atomic_units(history)
    assert len(units) == 1
    assert "OPAQUE_CIPHER" not in units[0].source_text
    request = ContextReclaimRequest(route_fp="fixture", round_id="one",
        transcript_sha256=cc.context_reclaim_transcript_sha256(history),
        measurement_basis="cold_estimate", measurement_density=1.0, reclaim_goal_tokens=1)
    selection = cc._Selection(units, "a" * 64, 0)
    ref = cc._persist_reclaim_checkpoint(history, request, selection, drive_root=tmp_path, task_id="checkpoint")
    assert ref is not None
    retained = json.loads(read_actor_source_bytes(tmp_path, "checkpoint", ref))
    assert retained["messages"] == history
    manifest = json.loads(next((tmp_path / "observability" / "calls" / "checkpoint").glob("*.json")).read_text())
    private = read_blob_ref(tmp_path, manifest["full_payload_ref"])
    public = read_blob_ref(tmp_path, manifest["redacted_projection_ref"])
    assert private["messages"] == history
    assert "OPAQUE_CIPHER" not in json.dumps(public)
    assert custody.replay_items(retained["messages"][0], target()) is not None
