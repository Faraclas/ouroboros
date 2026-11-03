"""Copilot non-stream Chat blocks, not alternative indexed completions.

The four-call shape/ids/arguments below are recorded from the HTTP-200 body of
2026-11-03's Copilot diagnostic (decoded body SHA256
22260ca1324b37781bc55f6f5a66dae1aeb4a674f85ef507abe12fbb995898ac).
Other mutations are synthetic falsifiers, not claims about provider behavior.
All sends use the real installed OpenAI SDK against an in-memory HTTP peer.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import openai
import pytest

from ouroboros.llm import LLMClient
from ouroboros.llm_openai_compatible import _copilot_block_choice
from ouroboros.llm_stream import RejectedProviderStream
from ouroboros.loop_llm_call import classify_llm_exception
from ouroboros.request_wire_recovery import plan_next_wire_retry
from tests._usage_store_testing import ledger_rows
from tests.test_transport_b_stream_deadlines import isolated as isolated

HOST = "https://api.business.githubcopilot.com"
MODEL = "openai-compatible::claude-opus-5.5"
KEYS = ["alpha", "beta", "gamma", "delta"]
IDS = ["toolu_01LmCHV6zMMkTyyFc1kiriYv", "toolu_01VkJ7QX9PJfjwt4gWp7Gvo9",
       "toolu_014Ze83imvHzZzTefQRwCmRe", "toolu_015wHgg2TZyxPXSyXoYHRM3v"]
TOOLS = [{"type": "function", "function": {"name": "lookup_value", "parameters": {
    "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}}]


def recorded_body():
    return {"id": "msg_011CggkFmrtLkgMd71TDukqC", "created": 1793743218,
            "model": "claude-opus-5.5", "choices": [
                {"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
                    {"id": identifier, "type": "function", "function": {
                        "name": "lookup_value", "arguments": json.dumps({"key": key}, separators=(",", ":"))}}]}}
                for key, identifier in zip(KEYS, IDS)],
            "usage": {"completion_tokens": 193, "prompt_tokens": 475, "total_tokens": 668,
                      "prompt_tokens_details": {"cached_tokens": 0}}}


def target(base_url=HOST, provider="openai-compatible"):
    return {"provider": provider, "base_url": base_url, "resolved_model": "claude-opus-5.5",
            "usage_model": MODEL, "supports_openrouter_extensions": False,
            "supports_generation_cost": False}


def normalize(body, route=None):
    return LLMClient()._normalize_remote_response(body, route or target(), skip_cost_fetch=True)


def _wire_stream(body):
    """Recorded Copilot stream structure: choice 0, indexed tools, terminal usage.

    Synthetic framing with the non-stream fixture's values. Parent independently
    recorded this indexed SSE shape; no unobserved unindexed stream is invented.
    """
    chunks = []
    for index, choice in enumerate(body["choices"]):
        call = copy.deepcopy(choice["message"]["tool_calls"][0])
        call["index"] = index
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]})
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                   "usage": body["usage"]})
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


@pytest.fixture
def route(monkeypatch, isolated):
    opened = []
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fixture-not-live")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", HOST)

    def make(body, *, asynchronous=False, no_proxy=False, stream=False):
        sent, dumps = [], []
        client = LLMClient()

        def respond(request):
            payload = json.loads(request.content)
            sent.append(payload)
            if len(sent) == 1:
                if stream:
                    return httpx.Response(200, content=_wire_stream(body),
                                          headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"id": "round-two", "model": "claude-opus-5.5",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "all four received"},
                             "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 4}})

        transport = httpx.MockTransport(respond)
        http = (httpx.AsyncClient if asynchronous else httpx.Client)(transport=transport)
        sdk = (openai.AsyncOpenAI if asynchronous else openai.OpenAI)(
            api_key="fixture-not-live", base_url=HOST, max_retries=0, http_client=http)
        opened.append(sdk)
        if no_proxy:
            monkeypatch.setattr(client, "_make_no_proxy_async_client" if asynchronous else "_make_no_proxy_client",
                                lambda *_a, **_kw: (sdk, http))
        else:
            monkeypatch.setattr(client, "_get_async_remote_client" if asynchronous else "_get_remote_client",
                                lambda *_a, **_kw: sdk)
        original = client._normalize_remote_response

        def observe(body, *args, **kwargs):
            # This seam is AFTER the returned SDK response was accounted, not
            # inside a transport closure that might retry a failed projection.
            assert ledger_rows(isolated)[-1]["state"] == "settled"
            dumps.append(copy.deepcopy(body))
            result = original(body, *args, **kwargs)
            assert body == dumps[-1]
            return result

        monkeypatch.setattr(client, "_normalize_remote_response", observe)
        return client, sent, dumps

    yield make
    for sdk in opened:
        if isinstance(sdk, openai.AsyncOpenAI):
            asyncio.run(sdk.close())
        else:
            sdk.close()


def chat(client, messages, *, asynchronous=False, **kwargs):
    call = dict(messages=messages, model=MODEL, tools=TOOLS, reasoning_effort="high", max_tokens=1024, **kwargs)
    return asyncio.run(client.chat_async(**call)) if asynchronous else client.chat(**call)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("no_proxy", [False, True], ids=["cached", "no-proxy"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
def test_actual_sdk_and_llmclient_keep_all_tools_and_replay_results(route, isolated, asynchronous, no_proxy, stream):
    client, sent, dumps = route(recorded_body(), asynchronous=asynchronous, no_proxy=no_proxy, stream=stream)
    history = [{"role": "user", "content": "look up alpha, beta, gamma, delta"}]
    msg, usage = chat(client, history, asynchronous=asynchronous, no_proxy=no_proxy, stream=stream)
    calls = msg["tool_calls"]
    assert [call["id"] for call in calls] == IDS
    assert [json.loads(call["function"]["arguments"])["key"] for call in calls] == KEYS
    assert usage["response_finish_reason"] == "tool_calls"
    assert usage["prompt_tokens"] == 475 and usage["completion_tokens"] == 193
    assert len(sent) == 1
    assert len(dumps[0]["choices"]) == (1 if stream else 4)
    if not stream:
        assert all(choice["index"] is None for choice in dumps[0]["choices"])
    history += [msg, *[{"role": "tool", "tool_call_id": call["id"], "content": "value:" + key}
                      for key, call in zip(KEYS, calls)]]
    if no_proxy:
        # The production no-proxy API owns/closes the per-call HTTP client.
        client, next_sent, _dumps = route(recorded_body(), asynchronous=asynchronous, no_proxy=True)
        # This second isolated peer is only a request observer; any returned tools
        # are irrelevant to the replay assertion, not a simulated final answer.
        chat(client, history, asynchronous=asynchronous, no_proxy=True)
        replay = next_sent[0]["messages"]
    else:
        final, _usage = chat(client, history, asynchronous=asynchronous)
        assert final["content"] == "all four received"
        assert len(sent) == 2
        replay = sent[1]["messages"]
    assistant = next(row for row in replay if row["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == IDS
    assert [row["tool_call_id"] for row in replay if row["role"] == "tool"] == IDS
    assert len(ledger_rows(isolated)) == 2


def test_text_refusal_and_every_continuation_block_survive_in_order(isolated):
    body = recorded_body()
    body["choices"].insert(0, {"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "First ", "refusal": "No ",
        "reasoning_text": "think ", "reasoning_opaque": "opaque-byte-for-byte",
        "cot_id": "cot-id", "future_continuation": {"signed": ["opaque", 7]},
        "reasoning_content": "provider text ",
        "reasoning_details": [{"type": "reasoning.encrypted", "data": "one", "signature": "sig"}]}})
    body["choices"][1]["message"].update(content="second.", refusal="thanks.", reasoning_text="then act",
        reasoning_content="then tool", reasoning_details=[{"type": "reasoning.text", "text": "two"}])
    original = copy.deepcopy(body)
    message, usage = normalize(body)
    assert body == original
    assert message["content"] == "First second."
    assert message["refusal"] == "No thanks."
    assert message["reasoning_text"] == "think then act"
    assert message["reasoning_content"] == "provider text then tool"
    assert message["reasoning_opaque"] == "opaque-byte-for-byte"
    assert message["cot_id"] == "cot-id"
    assert message["future_continuation"] == {"signed": ["opaque", 7]}
    assert message["reasoning_details"] == [
        {"type": "reasoning.encrypted", "data": "one", "signature": "sig"},
        {"type": "reasoning.text", "text": "two"}]
    assert [call["id"] for call in message["tool_calls"]] == IDS
    assert usage["response_finish_reason"] == "tool_calls"


def test_mixed_text_and_structured_content_are_lossless(isolated):
    body = recorded_body()
    body["choices"][0]["message"]["content"] = "before"
    body["choices"][1]["message"]["content"] = [{"type": "text", "text": "middle"},
                                               {"type": "future", "opaque": "keep"}]
    body["choices"][2]["message"]["content"] = "after"
    message, _ = normalize(body)
    assert message["content"] == [{"type": "text", "text": "before"}, {"type": "text", "text": "middle"},
                                  {"type": "future", "opaque": "keep"}, {"type": "text", "text": "after"}]


def test_documented_opaque_and_cot_id_are_latest_value_not_concatenated(isolated):
    body = recorded_body()
    for index, choice in enumerate(body["choices"]):
        choice["message"].update(reasoning_opaque=f"opaque-{index}", cot_id=f"cot-{index}",
                                 cot_summary=f"summary-{index} ")
    message, _ = normalize(body)
    assert message["reasoning_opaque"] == "opaque-3"
    assert message["cot_id"] == "cot-3"
    assert message["cot_summary"] == "summary-0 summary-1 summary-2 summary-3 "


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_sdk_prose_reasoning_then_five_tools_is_not_a_terminal_answer(route, asynchronous):
    body = recorded_body()
    fifth = copy.deepcopy(body["choices"][-1])
    fifth["message"]["tool_calls"][0]["id"] = "toolu_fifth"
    fifth["message"]["tool_calls"][0]["function"]["arguments"] = '{"key":"epsilon"}'
    body["choices"].append(fifth)
    body["choices"].insert(0, {"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "I will read the five files.",
        "reasoning_text": "Checking all sources.\n\n", "reasoning_opaque": "opaque-signed-state", "cot_id": "cot-id"}})
    client, sent, dumps = route(body, asynchronous=asynchronous)
    message, usage = chat(client, [{"role": "user", "content": "read files"}], asynchronous=asynchronous)
    assert len(sent) == 1 and len(dumps[0]["choices"]) == 6
    assert message["content"] == "I will read the five files."
    assert message["reasoning_text"] == "Checking all sources.\n\n"
    assert message["reasoning_opaque"] == "opaque-signed-state"
    assert [call["id"] for call in message["tool_calls"]] == IDS + ["toolu_fifth"]
    assert usage["response_finish_reason"] == "tool_calls"
    history = [{"role": "user", "content": "read files"}, message, *[
        {"role": "tool", "tool_call_id": call["id"], "content": "file content"}
        for call in message["tool_calls"]]]
    chat(client, history, asynchronous=asynchronous)
    replay = next(row for row in sent[1]["messages"] if row["role"] == "assistant")
    for field in ("content", "reasoning_text", "reasoning_opaque", "cot_id", "tool_calls"):
        assert replay[field] == message[field]


def test_projection_is_not_selected_by_model_name_and_accepts_explicit_sdk_nulls(isolated):
    body = recorded_body()
    body["model"] = "gpt-6.1-sol"
    for choice in body["choices"]:
        choice["index"] = None
        choice["message"].update(content=None, refusal=None, reasoning_text=None)
    message, _ = normalize(body, {**target(), "resolved_model": "gpt-6.1-sol"})
    assert [call["id"] for call in message["tool_calls"]] == IDS


def test_null_finish_marker_is_preserved_instead_of_inventing_a_stop(isolated):
    body = recorded_body()
    for choice in body["choices"]:
        choice["finish_reason"] = None
    _message, usage = normalize(body)
    assert "response_finish_reason" in usage and usage["response_finish_reason"] is None


def test_equal_output_limit_marker_is_not_rewritten_as_tool_calls(isolated):
    body = recorded_body()
    for choice in body["choices"]:
        choice["finish_reason"] = "length"
    _message, usage = normalize(body)
    assert usage["response_finish_reason"] == "length"


@pytest.mark.parametrize("url", ["https://gateway.invalid/v1", "https://api.githubcopilot.com",
    "http://api.business.githubcopilot.com", "https://api.business.githubcopilot.com.evil.invalid",
    "https://api.business.githubcopilot.com@evil.invalid", "https://evil.invalid/api.business.githubcopilot.com"])
def test_foreign_routes_never_merge_even_claude_named_models(isolated, url):
    message, _ = normalize(recorded_body(), target(url))
    assert [call["id"] for call in message["tool_calls"]] == IDS[:1]


@pytest.mark.parametrize("provider", ["openai", "openrouter", "anthropic", "minimax"])
def test_other_provider_lanes_never_opt_into_dialect(isolated, provider):
    assert _copilot_block_choice(recorded_body(), target(provider=provider)) is None


def test_indexed_alternative_choices_are_not_combined(isolated):
    body = recorded_body()
    for index, choice in enumerate(body["choices"]):
        choice["index"] = index
    message, _ = normalize(body)
    assert [call["id"] for call in message["tool_calls"]] == IDS[:1]


def test_same_function_and_arguments_with_distinct_ids_remain_distinct(isolated):
    body = recorded_body()
    for choice in body["choices"]:
        choice["message"]["tool_calls"][0]["function"]["arguments"] = "{}"
    message, _ = normalize(body)
    assert [call["id"] for call in message["tool_calls"]] == IDS


def corrupt(body, case):
    choices = body["choices"]
    if case == "mixed-index":
        choices[-1]["index"] = 1
    elif case == "finish":
        choices[-1]["finish_reason"] = "stop"
    elif case == "opaque":
        choices[0]["message"]["future_signed_state"] = "one"
        choices[1]["message"]["future_signed_state"] = "two"
    elif case == "duplicate-id":
        choices[-1]["message"]["tool_calls"][0]["id"] = IDS[0]
    elif case == "missing-id":
        del choices[-1]["message"]["tool_calls"][0]["id"]
    elif case == "arguments-not-text":
        choices[-1]["message"]["tool_calls"][0]["function"]["arguments"] = {}
    elif case == "role":
        choices[-1]["message"]["role"] = "user"
    elif case == "nonmessage":
        choices[-1]["message"] = None
    elif case == "nonchoice":
        choices[-1] = None
    elif case == "legacy-function":
        choices[0]["message"]["function_call"] = {"name": "lookup_value", "arguments": "{}"}
    return body


@pytest.mark.parametrize("case", ["mixed-index", "finish", "opaque", "duplicate-id", "missing-id",
                                  "arguments-not-text", "role", "nonmessage", "nonchoice", "legacy-function"])
def test_ambiguous_blocks_reject_without_partial_message_or_retry(isolated, case):
    body = corrupt(recorded_body(), case)
    original = copy.deepcopy(body)
    with pytest.raises(RejectedProviderStream) as caught:
        normalize(body)
    assert body == original
    error = caught.value
    classification = classify_llm_exception(error)
    assert classification.kind == "provider_error" and not classification.retry_same_request
    assert error.stream_rejected and error.stream_incomplete
    assert not getattr(error, "code", "") and not getattr(error, "status_code", None)
    assert plan_next_wire_retry({}, error=error, target=target()) is None
    assert error.stream_usage == body["usage"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_sdk_completed_ambiguous_response_is_settled_once_and_never_resent(route, isolated, asynchronous):
    client, sent, dumps = route(corrupt(recorded_body(), "duplicate-id"), asynchronous=asynchronous)
    with pytest.raises(RejectedProviderStream):
        chat(client, [{"role": "user", "content": "lookup"}], asynchronous=asynchronous)
    assert len(sent) == len(dumps) == len(ledger_rows(isolated)) == 1
    assert ledger_rows(isolated)[0]["state"] == "settled"
    assert ledger_rows(isolated)[0]["completion_tokens"] == 193
