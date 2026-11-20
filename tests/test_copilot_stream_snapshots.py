"""Copilot opaque snapshots through the real SDK, accounting and next send.

HTTP is entirely in-memory. Values are synthetic contract falsifiers, not
observations of current provider traffic.
"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
from types import SimpleNamespace

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


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("source_model,destination_model,change", [
    ("gemini-4-neon", "claude-opus-5.5", "model"),
    ("claude-opus-5.5", "gemini-4-neon", "model"),
    ("claude-opus-5.5", "claude-sonnet-5.5", "model"),
    ("gemini-4-neon", "gpt-6.1-sol", "model"),
    ("gpt-6.1-sol", "claude-opus-5.5", "model"),
    ("gemini-4-neon", "gemini-4-neon", "credential"),
    ("gemini-4-neon", "gemini-4-neon", "endpoint"),
    ("gemini-4-neon", "gemini-4-neon", "unbound"),
    ("gemini-4-neon", "gemini-4-neon", "same"),
], ids=["gemini-to-claude", "claude-to-gemini", "claude-to-claude",
        "gemini-to-gpt", "gpt-to-claude", "same-model-credential",
        "same-model-endpoint", "legacy-unbound-history", "same-binding"])
def test_fallback_final_wire_and_next_round(monkeypatch, isolated, asynchronous, source_model, destination_model, change):
    from ouroboros import net_transport
    from ouroboros.copilot_native_custody import CHAT_REASONING_BINDING_KEY
    from ouroboros.loop_model_call import _adopt_fallback_route

    def deny_network(*args, **kwargs):
        raise AssertionError("only mocked inference HTTP is allowed")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", HOST)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fixture-primary")
    monkeypatch.setattr("ouroboros.llm.in_worker_process", lambda: False)
    hm = net_transport._openai_httpx_module()
    sent = []
    candidates = []

    def wire_answer(request):
        sent.append((request, json.loads(request.content)))
        ordinal = len(sent)
        call_id = f"call-{ordinal}"
        body = sent[-1][1]
        if request.url.path.endswith("/responses"):
            return hm.Response(200, json={"id": f"response-{ordinal}", "model": body["model"],
                "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 5}, "output": [
                    {"type": "reasoning", "id": f"reason-{ordinal}", "summary": [],
                     "encrypted_content": f"native-private-{ordinal}"},
                    {"type": "message", "role": "assistant", "status": "completed", "phase": "commentary",
                     "content": [{"type": "output_text", "text": f"visible-{ordinal}"}]},
                    {"type": "function_call", "id": f"item-{ordinal}", "status": "completed",
                     "call_id": call_id, "name": "lookup", "arguments": "{}"},
                ]})
        return hm.Response(200, json={"id": f"response-{ordinal}", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": f"visible-{ordinal}",
                CHAT_REASONING_BINDING_KEY: "untrusted provider label",
                **{name: f"{name}-private-{ordinal}" for name in ("reasoning_opaque", "reasoning_text", "cot_id", "cot_summary")},
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            }}], "usage": {"prompt_tokens": 30, "completion_tokens": 5}})

    client = LLMClient()
    sdks = {}
    build = client._build_remote_kwargs

    def observe_candidate(*args, **kwargs):
        candidate = build(*args, **kwargs)
        candidates.append(copy.deepcopy(candidate["messages"]))
        return candidate

    monkeypatch.setattr(client, "_build_remote_kwargs", observe_candidate)

    def mocked_sdk(target):
        key = (target["base_url"], target["api_key"])
        if key not in sdks:
            http = (hm.AsyncClient if asynchronous else hm.Client)(transport=hm.MockTransport(wire_answer))
            cls = openai.AsyncOpenAI if asynchronous else openai.OpenAI
            remote = cls(base_url=key[0], api_key=key[1], max_retries=0, http_client=http)
            sdks[key] = net_transport.attach_remote_client(remote, async_client=asynchronous)
        return sdks[key]

    monkeypatch.setattr(client, "_get_async_remote_client" if asynchronous else "_get_remote_client", mocked_sdk)
    source = f"openai-compatible::{source_model}"
    destination = f"openai-compatible::{destination_model}"

    def add_tool_episode(history, answer, n):
        assert answer["content"] == f"visible-{n}"
        assert answer["tool_calls"][0]["id"] == f"call-{n}"
        history.extend([answer, {"role": "tool", "tool_call_id": f"call-{n}", "content": f"result-{n}"}])

    async def run():
        async def send(history, model):
            if asynchronous:
                return (await client.chat_async(messages=history, model=model, max_tokens=1024))[0]
            return client.chat(messages=history, model=model, max_tokens=1024)[0]

        history = [{"role": "user", "content": "fixture"}]
        original_answer = await send(history, source)
        if not source_model.startswith("gpt-"):
            binding = original_answer.get(CHAT_REASONING_BINDING_KEY)
            assert isinstance(binding, dict) and binding.get("model") == source_model
            assert "fixture-primary" not in json.dumps(binding)
        if change == "unbound":
            original_answer.pop(CHAT_REASONING_BINDING_KEY, None)
        add_tool_episode(history, original_answer, 1)
        history[:] = json.loads(json.dumps(history))  # private checkpoint round-trip
        original = copy.deepcopy(history)
        if change == "credential":
            monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fixture-fallback")
        if change == "endpoint":
            monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", HOST + "/alternate")

        candidate = client.sanitize_reasoning_on_model_switch(history, source, destination)
        answer = await send(candidate, destination)
        assert history == original
        ctx = SimpleNamespace(active_model=source)
        _adopt_fallback_route(ctx, None, destination, False, history, candidate, None, "max", [], {})
        add_tool_episode(history, answer, 2)
        assert (await send(history, ctx.active_model))["content"] == "visible-3"

    async def close():
        for sdk in sdks.values():
            if asynchronous:
                await sdk.close()
            else:
                sdk.close()

    scope = ua.UsageScope(drive_root=isolated, task_id="fallback-fixture", root_task_id="fallback-fixture")
    try:
        with ua.usage_scope(scope):
            asyncio.run(run())
    finally:
        asyncio.run(close())
    assert len(sent) == len(candidates) == 3
    assert [body["model"] for _, body in sent] == [source_model, destination_model, destination_model]
    for ordinal, (request, body) in enumerate(sent[1:], 2):
        assert request.url.path.endswith("/responses" if destination_model.startswith("gpt-") else "/chat/completions")
        encoded = json.dumps(body)
        sdk_input = json.dumps(candidates[ordinal - 1])
        assert CHAT_REASONING_BINDING_KEY not in sdk_input
        assert CHAT_REASONING_BINDING_KEY not in encoded
        for n in range(1, ordinal):
            assert f"visible-{n}" in encoded
            if "input" in body:
                assert {"type": "function_call", "call_id": f"call-{n}", "name": "lookup", "arguments": "{}"} in body["input"]
                assert {"type": "function_call_output", "call_id": f"call-{n}", "output": f"result-{n}"} in body["input"]
            else:
                assistant = next(row for row in body["messages"] if row.get("content") == f"visible-{n}")
                assert assistant["tool_calls"][0]["id"] == f"call-{n}"
                assert {"role": "tool", "tool_call_id": f"call-{n}", "content": f"result-{n}"} in body["messages"]
            can_replay = n == 2 or change == "same"
            originating_model = source_model if n == 1 else destination_model
            values = [f"native-private-{n}"] if originating_model.startswith("gpt-") else [
                f"{name}-private-{n}" for name in ("reasoning_opaque", "reasoning_text", "cot_id", "cot_summary")]
            assert all((value in encoded) == can_replay for value in values)
            # The Responses codec alone would silently ignore foreign Chat keys.
            assert all((value in sdk_input) == can_replay for value in values)
    records = ledger_rows(isolated)
    assert len(records) == 3 and all(row["state"] == "settled" for row in records)


def test_bound_chat_reasoning_can_return_to_origin_after_a_cross_family_fallback():
    from ouroboros.anthropic_native_custody import scrub_native_custody
    from ouroboros.copilot_native_custody import (
        CHAT_REASONING_BINDING_KEY, CHAT_REASONING_FIELDS, bind_chat_reasoning, prepare_chat_replay,
    )

    client = LLMClient()
    target = {"provider": "openai-compatible", "base_url": HOST,
              "resolved_model": "gemini-4-neon", "api_key": "fixture-primary"}
    source = {"role": "assistant", "content": "visible", "reasoning_opaque": "private",
              "tool_calls": [{"id": "call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
    bind_chat_reasoning(source, target)
    assert CHAT_REASONING_BINDING_KEY in source
    history = [source, {"role": "tool", "tool_call_id": "call", "content": "result"}]
    switched = client.sanitize_reasoning_on_model_switch(history, "openai-compatible::gemini-4-neon", "anthropic::claude-opus-5.5")
    assert switched[0]["reasoning_opaque"] == "private"
    foreign_wire = scrub_native_custody(switched)
    assert not CHAT_REASONING_FIELDS.intersection(foreign_wire[0])
    assert CHAT_REASONING_BINDING_KEY not in foreign_wire[0]
    assert foreign_wire[1] == history[1]
    returned = client.sanitize_reasoning_on_model_switch(switched, "anthropic::claude-opus-5.5", "openai-compatible::gemini-4-neon")
    same_route_wire = scrub_native_custody(returned)
    prepare_chat_replay(returned, same_route_wire, target)
    assert same_route_wire[0]["reasoning_opaque"] == "private"
    assert same_route_wire[0]["tool_calls"] == source["tool_calls"]
    assert CHAT_REASONING_BINDING_KEY not in same_route_wire[0]


@pytest.mark.parametrize("part", ["text", "call", "private"])
def test_changed_chat_message_cannot_replay_its_old_private_continuation(part):
    from ouroboros.anthropic_native_custody import scrub_native_custody
    from ouroboros.copilot_native_custody import CHAT_REASONING_FIELDS, bind_chat_reasoning, prepare_chat_replay

    target = {"provider": "openai-compatible", "base_url": HOST,
              "resolved_model": "gemini-4-neon", "api_key": "fixture-primary"}
    source = {"role": "assistant", "content": "original", "reasoning_opaque": "private",
              "tool_calls": [{"id": "call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
    bind_chat_reasoning(source, target)
    if part == "text":
        source["content"] = "edited"
    elif part == "call":
        source["tool_calls"][0]["function"]["arguments"] = '{"changed":true}'
    else:
        source["reasoning_opaque"] = "changed"
    prepared = scrub_native_custody([source])
    prepare_chat_replay([source], prepared, target)
    assert not CHAT_REASONING_FIELDS.intersection(prepared[0])
    assert prepared[0]["content"] == source["content"]
    assert prepared[0]["tool_calls"] == source["tool_calls"]


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
