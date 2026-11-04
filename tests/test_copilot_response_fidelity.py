"""Offline Copilot bridge regressions through real SDK and shared consumers."""
import asyncio
import gzip
import json
from unittest.mock import patch

import pytest

from ouroboros import net_transport as nt
from ouroboros._usage_response import usage_from_response
from ouroboros.llm import LLMClient
from ouroboros.llm_stream import ChatAccumulator
from ouroboros.loop_llm_call import classify_llm_exception


def native(status="completed", output=None, **extra):
    return {"id": "resp_fixture", "model": "gpt-6.1-sol", "status": status,
            "output": output if output is not None else [message("OK")], **extra}


def message(text):
    return {"type": "message", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def call(status="completed"):
    return {"type": "function_call", "status": status, "call_id": "c1",
            "name": "read_file", "arguments": '{"path":"fixture.txt"}'}


def sdk_reply(body, *, asynchronous=False, stream=False, status=200, compressed=False):
    """Use the production-selected httpx backend, SDK and wrapper; no socket."""
    hm = nt._openai_httpx_module()
    seen = []
    data = json.dumps(body).encode()
    headers = {"x-request-id": "fixture-id", "retry-after": "7", "content-type": "application/json"}
    if compressed:
        data = gzip.compress(data)
        headers["content-encoding"] = "gzip"

    def handler(request):
        seen.append(request)
        return hm.Response(status, content=data, headers=headers)

    transport = hm.MockTransport(handler)
    import openai
    kwargs = dict(api_key="offline-fixture", base_url="https://api.githubcopilot.com", max_retries=0)
    request = dict(model="gpt-6.1-sol", messages=[{"role": "user", "content": "fixture"}], stream=stream)
    if asynchronous:
        async def run():
            async with nt.attach_copilot_client(openai.AsyncOpenAI(http_client=hm.AsyncClient(transport=transport), **kwargs), async_client=True) as client:
                result = await client.chat.completions.create(**request)
                if stream:
                    return [chunk.model_dump() async for chunk in result]
                return result.model_dump()
        result = asyncio.run(run())
    else:
        with nt.attach_copilot_client(openai.OpenAI(http_client=hm.Client(transport=transport), **kwargs)) as client:
            result = client.chat.completions.create(**request)
            result = [chunk.model_dump() for chunk in result] if stream else result.model_dump()
    assert len(seen) == 1 and seen[0].url.path == "/responses"
    if stream:
        accumulator = ChatAccumulator()
        for chunk in result:
            accumulator.accept("", json.dumps(chunk))
        accumulator.accept("", "[DONE]")
        result = accumulator.result()
    return result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_completed_roundtrip_refusal_and_nullable_usage(asynchronous, stream):
    body = native(output=[message("first"), call(), message("last")])
    result = sdk_reply(body, asynchronous=asynchronous, stream=stream, compressed=True)
    msg, usage = LLMClient()._normalize_remote_response(result, {
        "provider": "openai-compatible", "resolved_model": "gpt-6.1-sol"}, skip_cost_fetch=True)
    assert msg["content"] == "firstlast"
    assert msg["tool_calls"][0]["id"] == "c1"
    reported, cost, final = usage_from_response(result)
    assert reported["prompt_tokens"] is None and reported["completion_tokens"] is None
    assert reported["cached_tokens"] is None and cost is None and not final
    refusal = native(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "Cannot do that"}]}])
    with pytest.raises(Exception) as caught:
        sdk_reply(refusal, asynchronous=asynchronous, stream=stream)
    assert nt.copilot_response_failure(caught.value).body == refusal


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("body", [native("failed", [message("PARTIAL"), call()], error={"code": "server_error", "message": "temporarily unavailable"}),
    native("cancelled"), native("incomplete", [call()], incomplete_details={"reason": "content_filter"}),
    native("completed", [call("incomplete")])])
def test_sdk_terminal_failure_is_recoverable_typed_fact(body, asynchronous, stream):
    with pytest.raises(Exception) as caught:
        sdk_reply(body, asynchronous=asynchronous, stream=stream)
    failure = nt.copilot_response_failure(caught.value)
    assert isinstance(failure, nt.CopilotRejectedResponse)
    assert failure.body == body
    classification = classify_llm_exception(failure)
    assert classification.kind == "provider_error" and not classification.retry_same_request
    assert failure.stream_usage["prompt_tokens"] is None


@pytest.mark.parametrize("status", ["queued", "in_progress", None])
def test_nonterminal_is_unknown(status):
    with pytest.raises(nt.CopilotPendingResponse) as caught:
        nt._copilot_responses_to_chat_completion(native(status), "gpt-6.1-sol")
    assert classify_llm_exception(caught.value).kind == "provider_outcome_unknown"


def test_exhaustion_cannot_dispatch_valid_json_tool():
    result = sdk_reply(native("incomplete", [message("PARTIAL"), call()], incomplete_details={"reason": "max_output_tokens"}), stream=True)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == "PARTIAL" and not choice["message"].get("tool_calls")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_error_status_headers_and_body(asynchronous, compressed):
    import openai
    with pytest.raises(openai.RateLimitError) as caught:
        sdk_reply({"error": {"message": "fixture", "code": "rate_limit"}}, status=429,
                  asynchronous=asynchronous, compressed=compressed)
    assert caught.value.status_code == 429
    assert caught.value.response.headers["retry-after"] == "7"
    assert caught.value.response.headers["x-request-id"] == "fixture-id"
    assert caught.value.body["code"] == "rate_limit"


def test_zero_counts_extensions_and_main_refusal(tmp_path):
    result = sdk_reply(native(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}, "cost": 0}))
    reported, cost, final = usage_from_response(result)
    assert reported["prompt_tokens"] == 0 and reported["cached_tokens"] == 0
    assert cost == 0 and final
    from ouroboros import loop_llm_call as lc
    with pytest.raises(nt.CopilotRejectedResponse) as caught:
        nt._copilot_responses_to_chat_completion(native(output=[{"type": "message", "content": [
            {"type": "refusal", "refusal": "No."}]}]), "gpt-6.1-sol")
    assert caught.value.body["output"][0]["content"][0]["refusal"] == "No."
    body = nt._copilot_responses_to_chat_completion(native(output=[message("OK")]), "gpt-6.1-sol")
    msg, usage = LLMClient()._normalize_remote_response(body, {"provider": "openai-compatible", "resolved_model": "gpt-6.1-sol"}, skip_cost_fetch=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    with patch.object(lc, "_send_main_candidate", return_value=(msg, usage)) as send, patch.object(lc, "_prepare_main_messages", side_effect=lambda messages, **kw: messages):
        result, _ = lc.call_llm_with_retry(LLMClient(), [{"role": "user", "content": "fixture"}],
            "openai-compatible::gpt-6.1-sol", None, "high", 1, logs, "offline-fixture", 1, None, {},
            initial_messages=[{"role": "user", "content": "fixture"}])
    assert result["content"] == "OK" and send.call_count == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", ["failed", "in_progress"])
def test_physical_driver_preserves_terminal_separately_from_unknown_money(tmp_path, monkeypatch, asynchronous, stream, status):
    import openai
    from ouroboros import usage_accounting as ua
    import ouroboros.request_wire_contract as wire
    import ouroboros.pricing as pricing
    from ouroboros.transport_custody import outcome_unknown_on_chain
    from tests._usage_store_testing import ledger_rows
    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(LLMClient, "_get_supported_parameters", lambda *a, **k: None)
    hm = nt._openai_httpx_module()
    sends = []
    body = native(status, [message("PARTIAL"), call()], **({"error": {"code": "server_error", "message": "rate limit; unsupported cache; try again"}} if status == "failed" else {}))
    def handler(request):
        sends.append(request)
        return hm.Response(200, json=body)
    transport = hm.MockTransport(handler)
    route = {"provider": "openai-compatible", "resolved_model": "gpt-6.1-sol", "usage_model": "gpt-6.1-sol",
             "base_url": "https://api.githubcopilot.com", "api_key": "fixture", "supports_openrouter_extensions": False}
    kwargs = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "fixture"}],
              "max_tokens": 1024, "reasoning_effort": "high", "stream": stream}
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id="bridge-fixture", root_task_id="bridge-fixture")):
        with pytest.raises((nt.CopilotRejectedResponse, nt.CopilotPendingResponse)) as caught:
            if asynchronous:
                async def run():
                    async with openai.AsyncOpenAI(api_key="fixture", base_url=route["base_url"], max_retries=0,
                                                 http_client=hm.AsyncClient(transport=transport)) as client:
                        nt.attach_copilot_client(client, async_client=True)
                        return await LLMClient()._create_chat_completion_with_retries_async(client.chat.completions.create, kwargs, route)
                asyncio.run(run())
            else:
                with openai.OpenAI(api_key="fixture", base_url=route["base_url"], max_retries=0,
                                   http_client=hm.Client(transport=transport)) as client:
                    nt.attach_copilot_client(client)
                    LLMClient()._create_chat_completion_with_retries(client.chat.completions.create, kwargs, route)
    assert len(sends) == 1
    assert outcome_unknown_on_chain(caught.value) is (status == "in_progress")
    row = ledger_rows(tmp_path)[-1]
    assert row["state"] == ("settled" if status == "failed" else "unresolved")
    assert row.get("cost_usd") is None and not row.get("cost_final", False)
    assert row.get("prompt_tokens") is None and row.get("completion_tokens") is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_read_fault_cleanup_keeps_primary_error(asynchronous):
    hm = nt._openai_httpx_module()
    closed = []
    import openai
    request = hm.Request("POST", "https://api.githubcopilot.com/chat/completions", json={"model": "gpt-6.1-sol"})
    if asynchronous:
        class Broken(hm.AsyncByteStream):
            async def __aiter__(self):
                yield b"{"
                raise asyncio.CancelledError()
            async def aclose(self):
                closed.append(True)
        async def run():
            http = hm.AsyncClient(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Broken())))
            client = nt.attach_copilot_client(openai.AsyncOpenAI(api_key="fixture", base_url="https://api.githubcopilot.com", http_client=http), async_client=True)
            try:
                with pytest.raises(asyncio.CancelledError):
                    await http.send(request, stream=True)
            finally:
                await client.close()
        asyncio.run(run())
    else:
        class Broken(hm.SyncByteStream):
            def __iter__(self):
                yield b"{"
                raise hm.ReadError("primary read failure")
            def close(self):
                closed.append(True)
        http = hm.Client(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Broken())))
        client = nt.attach_copilot_client(openai.OpenAI(api_key="fixture", base_url="https://api.githubcopilot.com", http_client=http))
        try:
            with pytest.raises(hm.ReadError, match="primary read failure"):
                http.send(request, stream=True)
        finally:
            client.close()

    assert closed


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 9}, {"input_tokens": None, "output_tokens": 2},
    {"input_tokens": 8, "output_tokens": 2, "input_tokens_details": {}, "output_tokens_details": {}}])
def test_partial_usage_remains_partial(usage):
    result = nt._copilot_responses_to_chat_completion(native(usage=usage), "gpt-6.1-sol")
    reported, _, _ = usage_from_response(result)
    assert reported["prompt_tokens"] == (usage or {}).get("input_tokens")
    assert reported["completion_tokens"] == (usage or {}).get("output_tokens")
    assert reported["cached_tokens"] is None
    assert result["usage"].get("total_tokens") is None


def test_main_exhaustion_never_accepts_partial_tool(tmp_path):
    from ouroboros import loop_llm_call as lc
    body = nt._copilot_responses_to_chat_completion(native("incomplete", [call()],
        incomplete_details={"reason": "max_output_tokens"}), "gpt-6.1-sol")
    msg, usage = LLMClient()._normalize_remote_response(body, {"provider": "openai-compatible", "resolved_model": "gpt-6.1-sol"}, skip_cost_fetch=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    with patch.object(lc, "_send_main_candidate", return_value=(msg, usage)) as send, patch.object(lc, "_prepare_main_messages", side_effect=lambda messages, **kw: messages):
        result, _ = lc.call_llm_with_retry(LLMClient(), [{"role": "user", "content": "fixture"}],
            "openai-compatible::gpt-6.1-sol", None, "high", 1, logs, "offline-fixture", 1, None, {},
            initial_messages=[{"role": "user", "content": "fixture"}])
    assert result is None and send.call_count == 1


def test_real_file_result_replay(tmp_path):
    from ouroboros.tools.core import _read_file
    from ouroboros.tools.tool_context import ToolContext
    path = tmp_path / "fixture.txt"
    path.write_text("actual fixture text")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    text = _read_file(ctx, path="fixture.txt")
    assert "actual fixture text" in text
    completion = nt._copilot_responses_to_chat_completion(native(output=[call()]), "gpt-6.1-sol")
    assistant = completion["choices"][0]["message"]
    payload = nt._copilot_chat_to_responses_payload({"model": "gpt-6.1-sol", "messages": [assistant,
        {"role": "tool", "tool_call_id": "c1", "content": text}]})
    assert payload["input"][0]["call_id"] == payload["input"][1]["call_id"] == "c1"
    assert payload["input"][1]["output"] == text


def test_error_without_status_is_known_rejection():
    with pytest.raises(nt.CopilotRejectedResponse):
        nt._copilot_responses_to_chat_completion({"error": {"code": "server_error"}}, "gpt-6.1-sol")


def test_production_no_proxy_client_disables_sdk_retries(monkeypatch):
    hm = nt._openai_httpx_module()
    monkeypatch.setattr(nt, "remote_httpx_transport", lambda **kw: hm.MockTransport(lambda req: hm.Response(200, json={})))
    client, http_client = nt.make_no_proxy_client({"api_key": "fixture", "base_url": "https://api.githubcopilot.com"}, 30)
    try:
        assert client.max_retries == 0
    finally:
        http_client.close()


@pytest.mark.parametrize("field,value", [
    ("call_id", None), ("call_id", ""), ("call_id", 2),
    ("name", None), ("name", ""), ("name", {}),
    ("arguments", None), ("arguments", {}), ("arguments", 0), ("arguments", []),
])
@pytest.mark.parametrize("stream", [False, True])
def test_native_call_never_invents_structural_fields(field, value, stream):
    item = {**call(), "id": "fc_item_not_call_identity", field: value}
    with pytest.raises(Exception) as caught:
        sdk_reply(native(output=[item]), stream=stream)
    failure = nt.copilot_response_failure(caught.value)
    assert isinstance(failure, nt.CopilotRejectedResponse)
    assert failure.body["output"] == [item]
    del item[field]
    with pytest.raises(nt.CopilotRejectedResponse):
        nt._copilot_responses_to_chat_completion(native(output=[item]), "gpt-6.1-sol")


@pytest.mark.parametrize("arguments", ["{}", "", "not JSON", "[1]", '{"x":1}'])
@pytest.mark.parametrize("stream", [False, True])
def test_observed_strings_and_real_call_identity_survive(arguments, stream):
    item = {**call(), "id": "fc_item", "arguments": arguments}
    result = sdk_reply(native(output=[item, item]), stream=stream)
    calls = result["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 2  # Do not impose a new duplicate-ID policy here.
    assert all(c["id"] == "c1" and c["function"]["arguments"] == arguments for c in calls)
    replay = nt._copilot_chat_to_responses_payload({"model": "gpt-6.1-sol", "messages": [
        result["choices"][0]["message"], {"role": "tool", "tool_call_id": "c1", "content": "result"}]})
    assert all(c["arguments"] == arguments for c in replay["input"] if c.get("type") == "function_call")


def test_output_limit_does_not_validate_or_execute_partial_call():
    body = native("incomplete", [message("kept"), {"type": "function_call", "arguments": None}],
                  incomplete_details={"reason": "max_output_tokens"})
    result = sdk_reply(body)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == "kept"
    assert not choice["message"].get("tool_calls")


@pytest.mark.parametrize("status", ["failed", "in_progress"])
def test_provider_probe_unwraps_inside_physical_accounting(tmp_path, monkeypatch, status):
    import openai
    from tests._usage_store_testing import ledger_rows
    from ouroboros import usage_accounting as ua
    import ouroboros.pricing as pricing
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    hm = nt._openai_httpx_module()
    sends = []
    def handler(request):
        sends.append(request)
        return hm.Response(200, json=native(status))
    transport = hm.MockTransport(handler)
    remote = openai.OpenAI(api_key="fixture", base_url="https://api.githubcopilot.com",
                          max_retries=0, http_client=hm.Client(transport=transport))
    nt.attach_copilot_client(remote)
    client = LLMClient()
    monkeypatch.setattr(client, "_new_remote_client", lambda _target: remote)
    result = client.probe_provider_readiness("openai-compatible::gpt-6.1-sol", settings={
        "OPENAI_COMPATIBLE_API_KEY": "fixture", "OPENAI_COMPATIBLE_BASE_URL": "https://api.githubcopilot.com"})
    assert len(sends) == 1 and not result["ok"]
    assert result["exception_type"] == ("CopilotRejectedResponse" if status == "failed" else "CopilotPendingResponse")
    assert result["error"] == ("Provider response rejected" if status == "failed" else "Provider outcome unknown")
    rows = ledger_rows(tmp_path)
    row = rows[-1]
    assert row["state"] == ("settled" if status == "failed" else "unresolved")
    assert row.get("cost_usd") is None and not row.get("cost_final", False)


@pytest.mark.parametrize("outcome", ["completed", "failed", "refusal", "in_progress", "exhausted", "length_text", "chat_length", "length_marker"])
def test_real_loop_registry_tool_backflow(tmp_path, monkeypatch, provider_catalog_offline, outcome):
    import queue
    import openai
    from ouroboros import loop, usage_accounting as ua
    from ouroboros.tools.registry import ToolRegistry
    from tests._shared import saved_settings
    from tests._usage_store_testing import ledger_rows
    import ouroboros.request_wire_contract as wire
    import ouroboros.pricing as pricing

    saved_settings(monkeypatch, tmp_path, OPENAI_COMPATIBLE_API_KEY="fixture",
        OPENAI_COMPATIBLE_BASE_URL="https://api.githubcopilot.com",
        OUROBOROS_MODEL="openai-compatible::gpt-6.1-sol", OUROBOROS_MODEL_FALLBACKS="",
        OUROBOROS_TASK_REVIEW_MODE="off", OUROBOROS_SAFETY_MODE="off")
    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(LLMClient, "_get_supported_parameters", lambda *a, **k: None)
    (tmp_path / "fixture.txt").write_text("REAL_REGISTRY_NONCE_73f9")
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_model_override = "openai-compatible::gpt-6.1-sol"
    registry._ctx.task_use_local_override = False
    registry._ctx.exact_model_route = True
    # End only the offline wait, never replace send/normalization/tool dispatch.
    monkeypatch.setattr(loop, "_transport_wait_step", lambda *a, **k: False)
    client = LLMClient()
    hm = nt._openai_httpx_module()
    sent = []

    def handler(request):
        assert request.url.path == ("/chat/completions" if outcome == "chat_length" else "/responses")
        sent.append(json.loads(request.content))
        assert len(sent) <= 3, "Unexpected extra provider attempt"
        if outcome == "chat_length":
            chunk = {"id": "chat_fixture", "model": "claude-opus-5.5", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "PARTIAL" if len(sent) == 1 else "ROUNDTRIP_DONE"},
                 "finish_reason": "length" if len(sent) == 1 else "stop"}]}
            return hm.Response(200, content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
                               headers={"content-type": "text/event-stream"})
        if outcome == "completed":
            body = native(output=[call()] if len(sent) == 1 else [message("ROUNDTRIP_DONE")])
        elif outcome == "refusal":
            body = native(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}])
        elif outcome in {"exhausted", "length_text", "length_marker"}:
            body = (native("incomplete", [message("FINAL ANSWER: cut off" if outcome == "length_marker" else "PARTIAL"), *([call()] if outcome == "exhausted" else [])],
                           incomplete_details={"reason": "max_output_tokens"})
                    if len(sent) == 1 else native(output=[message("ROUNDTRIP_DONE")]))
        else:
            body = native("failed" if outcome == "exhausted" else outcome, [message("PARTIAL"), call()])
        return hm.Response(200, json=body)

    def fresh(target, timeout=None):
        transport = hm.MockTransport(handler)
        http = hm.Client(transport=transport)
        remote = openai.OpenAI(api_key="fixture", base_url="https://api.githubcopilot.com",
                              max_retries=0, http_client=http)
        nt.attach_copilot_client(remote)
        return remote, http

    remote, http = fresh({})
    monkeypatch.setattr(client, "_get_remote_client", lambda target: remote)
    monkeypatch.setattr(client, "_make_no_proxy_client", fresh)
    if outcome == "chat_length":
        registry._ctx.task_model_override = "openai-compatible::claude-opus-5.5"
    messages = [{"role": "user", "content": "Read fixture.txt and report completion."}]
    try:
        with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id="bridge-loop", root_task_id="bridge-loop")):
            answer, _usage, trace = loop.run_llm_loop(messages, registry, client, tmp_path,
                lambda *_a, **_kw: None, queue.Queue(), task_id="bridge-loop", drive_root=tmp_path,
                initial_effort="high")
    finally:
        http.close()
    if outcome in {"exhausted", "length_text", "chat_length", "length_marker"}:
        assert trace.get("best_valid_final_answer") != "cut off"
        assert answer == "ROUNDTRIP_DONE" and len(sent) == 2
        assert not trace["tool_calls"]
        history = sent[1]["messages" if outcome == "chat_length" else "input"]
        assert any(m.get("role") == "assistant" and m.get("content") == ("FINAL ANSWER: cut off" if outcome == "length_marker" else "PARTIAL") for m in history)
        assert any("This reply did not complete the task" in str(m.get("content", "")) for m in history)
        assert all("finish_reason" not in m for m in history)
        assert all(r["state"] == "settled" for r in ledger_rows(tmp_path))
        return
    if outcome != "completed":
        assert not trace["tool_calls"]
        assert answer not in {"PARTIAL", "ROUNDTRIP_DONE"}
        rows = ledger_rows(tmp_path)
        # Known rejected output may buy the existing, explicitly different
        # provider-unavailable terminal request. UNKNOWN buys no new generation.
        assert len(sent) == len(rows) == (1 if outcome == "in_progress" else 2), sent
        if len(sent) == 2:
            assert any("[PROVIDER_UNAVAILABLE]" in str(m.get("content", "")) for m in sent[1]["input"])
            assert sent[0]["input"] != sent[1]["input"]
        assert rows[0]["state"] == ("unresolved" if outcome == "in_progress" else "settled")
        return
    assert answer == "ROUNDTRIP_DONE" and len(sent) == 2
    assert any(t.get("name") == "read_file" for t in sent[0]["tools"])
    canonical = next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1")
    outputs = [m for m in sent[1]["input"] if m.get("type") == "function_call_output"]
    calls = [m for m in sent[1]["input"] if m.get("type") == "function_call"]
    assert len(outputs) == len(calls) == 1 and calls[0]["call_id"] == outputs[0]["call_id"] == "c1"
    assert outputs[0]["output"] == canonical["content"]
    assert "REAL_REGISTRY_NONCE_73f9" in outputs[0]["output"]
    assert len(trace["tool_calls"]) == 1
    assert len(ledger_rows(tmp_path)) == 2
    assert all(r["state"] == "settled" for r in ledger_rows(tmp_path))


def test_finish_fact_replaced_after_recovery_and_absence():
    from ouroboros.loop_llm_call import _take_response_facts

    total = {}
    _take_response_facts({"response_finish_reason": "length"}, {}, total)
    assert total["_response_finish_reason"] == "length"
    _take_response_facts({"response_finish_reason": "stop"}, {}, total)
    assert total["_response_finish_reason"] == "stop"
    _take_response_facts({}, {}, total)
    assert "_response_finish_reason" not in total
