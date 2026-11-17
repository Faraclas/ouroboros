"""Copilot opaque snapshots through the real SDK, accounting and next send.

HTTP is entirely in-memory. Values are synthetic contract falsifiers, not
observations of current provider traffic.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import openai
import pytest

from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from tests._usage_store_testing import ledger_rows
from tests.test_transport_b_stream_deadlines import isolated as isolated

HOST = "https://api.business.githubcopilot.com"
MODEL = "openai-compatible::claude-sonnet-5.5"


def wire():
    deltas = [
        {"role": "assistant", "content": "hello ", "reasoning_text": "first ",
         "reasoning_opaque": "opaque-A", "cot_id": "turn-A", "tool_calls": [
             {"index": 0, "id": "call-A", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"content": "world", "reasoning_text": "second", "reasoning_opaque": "opaque-B", "cot_id": "turn-B",
         "tool_calls": [{"index": 1, "id": "call-B", "type": "function",
                         "function": {"name": "lookup", "arguments": "{}"}}]},
        {"reasoning_opaque": None, "cot_id": None},
    ]
    chunks = [{"id": "snapshot-response", "model": "claude-sonnet-5.5", "choices": [
        {"index": 0, "delta": delta}]} for delta in deltas]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": 5}})
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("base_url,expected", [(HOST, "B"), ("https://provider.invalid/v1", "A+B")],
                         ids=["copilot", "foreign"])
def test_sdk_canonical_and_next_send_snapshots(monkeypatch, isolated, asynchronous, base_url, expected):
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fixture-not-live")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base_url)
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(200, content=wire(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "next-response", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "ACK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 1}})

    http = (httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(respond))
    sdk = (openai.AsyncOpenAI if asynchronous else openai.OpenAI)(
        api_key="fixture-not-live", base_url=base_url, max_retries=0, http_client=http)
    client = LLMClient()
    monkeypatch.setattr(client, "_get_async_remote_client" if asynchronous else "_get_remote_client",
                        lambda *_a, **_kw: sdk)
    history = [{"role": "user", "content": "preserve snapshots"}]
    original = copy.deepcopy(history)

    async def exercise_async():
        message, usage = await client.chat_async(messages=history, model=MODEL, stream=True, max_tokens=1024)
        history.append(message)
        history.extend({"role": "tool", "tool_call_id": identifier, "content": "fake-result"}
                       for identifier in ("call-A", "call-B"))
        history.append({"role": "user", "content": "next"})
        final, _ = await client.chat_async(messages=history, model=MODEL, max_tokens=1024)
        return message, usage, final

    scope = ua.UsageScope(drive_root=isolated, task_id="9d747c01", root_task_id="9d747c01",
                          billing_group_id="d43c0386", billing_group_limit_usd=50)
    try:
        with ua.usage_scope(scope):
            if asynchronous:
                message, usage, final = asyncio.run(exercise_async())
            else:
                message, usage = client.chat(messages=history, model=MODEL, stream=True, max_tokens=1024)
                history += [message, *[{"role": "tool", "tool_call_id": identifier, "content": "fake-result"}
                                      for identifier in ("call-A", "call-B")],
                            {"role": "user", "content": "next"}]
                final, _ = client.chat(messages=history, model=MODEL, max_tokens=1024)
        assert history[0] == original[0]
        assert message["content"] == "hello world"
        assert message["reasoning_text"] == "first second"
        opaque = "opaque-B" if expected == "B" else "opaque-Aopaque-B"
        cot = "turn-B" if expected == "B" else "turn-Aturn-B"
        assert message["reasoning_opaque"] == opaque
        assert message["cot_id"] == cot
        replay = next(row for row in sent[1]["messages"] if row["role"] == "assistant")
        assert replay["reasoning_opaque"] == opaque
        assert replay["cot_id"] == cot
        assert replay["content"] == message["content"]
        assert [call["id"] for call in message["tool_calls"]] == ["call-A", "call-B"]
        assert replay["tool_calls"] == message["tool_calls"]
        assert [row["tool_call_id"] for row in sent[1]["messages"] if row["role"] == "tool"] == ["call-A", "call-B"]
        assert final["content"] == "ACK" and len(sent) == 2
        assert usage["prompt_tokens"] == 40 and usage["completion_tokens"] == 5
        rows = ledger_rows(isolated)
        assert len(rows) == 2 and all(row["state"] == "settled" for row in rows)
        for row in rows:
            assert row["task_id"] == row["root_task_id"] == "9d747c01"
            assert row["billing_group_id"] == "d43c0386"
    finally:
        if asynchronous:
            asyncio.run(sdk.close())
        else:
            sdk.close()


@pytest.mark.parametrize("provider,base,model,selected", [
    ("openai-compatible", HOST, "claude-opus-5.5", True),
    ("openai-compatible", HOST, "claude-sonnet-5.5", True),
    ("openai-compatible", HOST, "gpt-6.1-sol", False),
    ("openai-compatible", "https://api.githubcopilot.com", "claude-sonnet-5.5", False),
    ("openai-compatible", "http://api.business.githubcopilot.com", "claude-sonnet-5.5", False),
    ("openai-compatible", HOST + ".evil.invalid", "claude-sonnet-5.5", False),
    ("anthropic", HOST, "claude-sonnet-5.5", False),
    ("openai", HOST, "claude-sonnet-5.5", False),
    ("openrouter", HOST, "claude-sonnet-5.5", False),
])
def test_exact_route_policy(provider, base, model, selected):
    from ouroboros.llm_openai_compatible import _copilot_chat_snapshot_fields

    fields = _copilot_chat_snapshot_fields({"provider": provider, "base_url": base, "resolved_model": model})
    assert fields == (frozenset({"reasoning_opaque", "cot_id"}) if selected else frozenset())


@pytest.mark.parametrize("latest", ["", "opaque-B"])
def test_only_top_level_snapshots_and_null_preserves(latest):
    from ouroboros.llm_stream import ChatAccumulator

    accumulator = ChatAccumulator(snapshot_fields=frozenset({"reasoning_opaque", "cot_id"}))
    for value in ["opaque-A", latest, None]:
        accumulator.accept("", json.dumps({"choices": [{"index": 0, "delta": {
            "reasoning_opaque": value, "cot_id": value, "content": "x",
            "future": {"reasoning_opaque": "fragment"}}}]}))
    accumulator.accept("", json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    accumulator.accept("", "[DONE]")
    message = accumulator.result()["choices"][0]["message"]
    assert message["reasoning_opaque"] == message["cot_id"] == latest
    assert message["content"] == "xxx"
    assert message["future"]["reasoning_opaque"] == "fragmentfragmentfragment"


@pytest.mark.parametrize("values", [["only"], ["same", "same"]])
def test_single_or_repeated_snapshot_is_not_appended(values):
    from ouroboros.llm_stream import ChatAccumulator

    accumulator = ChatAccumulator(snapshot_fields=frozenset({"reasoning_opaque", "cot_id"}))
    for value in values:
        accumulator.accept("", json.dumps({"choices": [{"index": 0, "delta": {
            "content": "x", "reasoning_opaque": value, "cot_id": value}}]}))
    accumulator.accept("", json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    accumulator.accept("", "[DONE]")
    message = accumulator.result()["choices"][0]["message"]
    assert message["reasoning_opaque"] == message["cot_id"] == values[-1]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("terminal", [False, True], ids=["incomplete", "provider-error"])
def test_snapshot_never_makes_partial_stream_usable(isolated, asynchronous, terminal):
    from ouroboros.llm_stream import IncompleteProviderStream, ProviderStreamError
    from tests.test_transport_b_stream_deadlines import WireResponse, run_driver, payload, sse, chunk, target

    frames = [chunk({"content": "partial", "reasoning_opaque": "A", "cot_id": "A"}),
              chunk({"reasoning_opaque": "B", "cot_id": "B"})]
    if terminal:
        frames.append({"error": {"message": "fixture rejection", "type": "invalid_request_error"},
                       "usage": {"prompt_tokens": 40, "completion_tokens": 5}})
    response = WireResponse(sse(*frames, done=False))
    route = {**target("openai-compatible"), "base_url": HOST, "resolved_model": "claude-sonnet-5.5"}
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return type("Stream", (), {"response": response})()

    with pytest.raises(ProviderStreamError if terminal else IncompleteProviderStream):
        run_driver(send, payload(stream=True), route, asynchronous=asynchronous)
    assert len(calls) == 1 and response.closed
    rows = ledger_rows(isolated)
    assert len(rows) == 1
    assert rows[0]["state"] == ("settled" if terminal else "unresolved")
