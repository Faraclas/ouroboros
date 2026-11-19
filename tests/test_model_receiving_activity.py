"""Text-free receipt facts at the real public client/parser boundary; all sends are scripted."""
import asyncio
import contextvars
from copy import deepcopy

import pytest

from ouroboros.llm import LLMClient
from ouroboros import llm_observability as activity
from tests._usage_store_testing import ledger_rows
from tests.test_copilot_responses_stream import exchange, event, terminal
from tests.test_copilot_response_fidelity import native, message
from tests.test_transport_b_stream_deadlines import (
    isolated as isolated, target, WireResponse, sse, chunk, native_events, MESSAGES,
)


def phases(rows):
    assert all(set(row) == {"model_activity"} and set(row["model_activity"]) == {"phase"} for row in rows)
    return [row["model_activity"]["phase"] for row in rows]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("route", ["responses", "claude-snapshot", "generic-chat"])
def test_public_first_output_precedes_terminal_not_headers_or_role(monkeypatch, isolated, asynchronous, route):
    observed = []
    if route == "responses":
        parts = [b": ping\n\n" + event("response.created", response={"id": "fixture"}),
                 event("response.output_text.delta", delta="PRIVATE-INERT-DELTA"),
                 terminal(native(output=[message("OK")]))]
        options = {}
    else:
        parts = [sse(chunk({"role": "assistant", "content": ""}), done=False),
                 sse(chunk({"content": "OK", **({"reasoning_opaque": "snapshot"} if route == "claude-snapshot" else {})}), done=False),
                 sse(chunk({}, "stop"))]
        options = {"model": "openai-compatible::claude-sonnet-5.5" if route == "claude-snapshot" else "openai-compatible::fixture"}
        if route == "generic-chat":
            options["base_url"] = "https://provider.invalid/v1"
    def read(index):
        # Called before each chunk is yielded to the actual SDK/native HTTP parser.
        expected = ["preparing", "awaiting_response"]
        if index == 2:
            expected.append("receiving")
        assert phases(observed) == expected
    (msg, _), sent, closed, reads = exchange(monkeypatch, isolated, parts, asynchronous=asynchronous,
        observer=observed.append, on_read=read, **options)
    assert msg["content"] == "OK"
    assert phases(observed) == ["preparing", "awaiting_response", "receiving", "response_received"]
    assert len(sent) == 1 and closed and len(reads) == 3
    assert ledger_rows(isolated)[0]["state"] == "settled"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("prefix", [
    b": ping\n\n", event("response.created", response={"id": "fixture"}),
    event("response.output_text.delta", delta="  \n"),
    event("response.output_text.delta", delta={"text": "not typed text"}),
    event("response.function_call_arguments.delta", delta=""),
    event("response.output_item.added", item={"type": "function_call", "name": "lookup", "arguments": ""}),
    event("response.usage", usage={"output_tokens": 30}),
])
def test_native_nonoutput_and_terminal_conversion_never_invent_receiving(monkeypatch, isolated, asynchronous, prefix):
    observed = []
    (msg, _), sent, *_ = exchange(monkeypatch, isolated, [prefix, terminal(native())],
        asynchronous=asynchronous, observer=observed.append)
    assert msg["content"] == "OK" and len(sent) == 1
    assert phases(observed) == ["preparing", "awaiting_response", "response_received"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind,fields", [
    ("response.reasoning_text.delta", {"delta": "hidden reason"}),
    ("response.reasoning_summary_text.delta", {"delta": "summary"}),
    ("response.function_call_arguments.delta", {"delta": '{"q":'}),
    ("response.custom_tool_call_input.delta", {"delta": "input"}),
    ("response.output_item.added", {"item": {"type": "reasoning", "encrypted_content": "opaque"}}),
])
def test_native_typed_nontext_output_is_receipt_only(monkeypatch, isolated, asynchronous, kind, fields):
    observed = []
    exchange(monkeypatch, isolated, [event(kind, **fields), terminal(native())],
        asynchronous=asynchronous, observer=observed.append)
    assert phases(observed) == ["preparing", "awaiting_response", "receiving", "response_received"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_native_anthropic_including_async_offload(monkeypatch, isolated, asynchronous):
    import requests
    events, expected = native_events()
    observed, sent = [], []
    output = WireResponse(sse(*events, done=False))
    def chunks(**kwargs):
        # First complete event is message_start; no generated output yet.
        yield sse(events[0], done=False)
        assert phases(observed) == ["preparing", "awaiting_response"]
        yield sse(*events[1:4], done=False)  # thinking delta, not the finished response
        assert phases(observed)[-1] == "receiving"
        yield sse(*events[4:], done=False)
    output.iter_content = chunks
    def post(*args, **kwargs):
        sent.append(deepcopy(kwargs))
        return output
    monkeypatch.setattr(requests, "post", post)
    client = LLMClient()
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: target("anthropic"))
    kwargs = dict(messages=MESSAGES, model="anthropic::fixture", stream=True, model_operation_observer=observed.append)
    msg, usage = asyncio.run(client.chat_async(**kwargs)) if asynchronous else client.chat(**kwargs)
    assert msg["content"] == "read this" and usage["completion_tokens"] == 12
    assert phases(observed) == ["preparing", "awaiting_response", "receiving", "response_received"]
    assert len(sent) == 1 and output.closed and ledger_rows(isolated)[0]["state"] == "settled"


@pytest.mark.parametrize("asynchronous,failure", [(asynchronous, failure)
    for asynchronous in (False, True) for failure in ("eof", "error", "observer", "cancel")
    if asynchronous or failure != "cancel"])
def test_public_output_failures_clear_receiving_without_regeneration(monkeypatch, isolated, asynchronous, failure):
    observed = []
    def observe(row):
        observed.append(deepcopy(row))
        if failure == "observer":
            raise RuntimeError("observer-only-sentinel")
    parts = [event("response.output_text.delta", delta="PRIVATE-INERT-DELTA")]
    options = {}
    if failure == "observer":
        parts.append(terminal(native()))
    elif failure == "error":
        parts.append(event("error", error={"type": "fixture_error", "message": "stream-sentinel"}))
    elif failure == "cancel":
        options["fault"] = asyncio.CancelledError()
    try:
        result, sent, *_ = exchange(monkeypatch, isolated, parts, asynchronous=asynchronous, observer=observe, **options)
    except BaseException as exc:
        assert failure != "observer"
        assert isinstance(exc, asyncio.CancelledError) if failure == "cancel" else isinstance(exc, Exception)
        assert phases(observed)[-1] == "unknown"
    else:
        assert failure == "observer" and result[0]["content"] == "OK" and len(sent) == 1
        assert phases(observed)[-1] == "response_received"
    assert "receiving" in phases(observed)
    assert len(ledger_rows(isolated)) == 1
    assert activity._MODEL_OBSERVER.get() is None and activity._PHYSICAL_ACTIVITY.get() is None


def test_attempt_reset_nested_calls_late_copy_and_observer_failure(monkeypatch):
    observed, nested, inherited = [], [], []
    sentinel = RuntimeError("original-send-exception")
    @activity.model_activity_scoped
    def invoke(model_operation_observer=None):
        @activity.physical_activity_scoped
        def attempt(fail=False):
            activity.observe_model_phase("awaiting_response")
            activity.observe_model_output(True)
            inherited.append(contextvars.copy_context())
            if fail:
                raise sentinel
            return "accepted"
        with pytest.raises(RuntimeError) as caught:
            attempt(True)
        assert caught.value is sentinel
        # Native-reader suppression is attempt-local, not inherited by the next attempt.
        @activity.model_activity_scoped
        def inner(model_operation_observer=None):
            return attempt()
        assert inner(model_operation_observer=nested.append) == "accepted"
        assert attempt() == "accepted"
    invoke(model_operation_observer=observed.append)
    assert phases(observed) == ["preparing", "awaiting_response", "receiving", "unknown",
                                "preparing", "awaiting_response", "receiving", "response_received"]
    assert phases(nested) == ["preparing", "awaiting_response", "receiving", "response_received"]
    before = deepcopy(observed), deepcopy(nested)
    for copied in inherited:
        copied.run(activity.observe_model_output, True)
        copied.run(activity.observe_model_phase, "awaiting_response")
    assert (observed, nested) == before
    assert activity._MODEL_OBSERVER.get() is None and activity._PHYSICAL_ACTIVITY.get() is None


def test_async_concurrent_invocations_do_not_cross_observers():
    outputs = [[], []]
    @activity.model_activity_scoped
    async def invoke(model_operation_observer=None):
        @activity.physical_activity_scoped
        async def physical():
            activity.observe_model_phase("awaiting_response")
            await asyncio.sleep(0)
            activity.observe_model_output(True)
            await asyncio.sleep(0)
        await physical()
    async def run():
        await asyncio.gather(*(invoke(model_operation_observer=rows.append) for rows in outputs))
    asyncio.run(run())
    assert all(phases(rows) == ["preparing", "awaiting_response", "receiving", "response_received"] for rows in outputs)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("delta,early", [
    ({"reasoning_text": "readable reasoning"}, True),
    ({"reasoning_opaque": "opaque-snapshot"}, True),
    ({"cot_id": "identifier-only"}, False),
    ({"reasoning_opaque": " \n"}, False),
    ({"reasoning_details": [{"type": "reasoning.encrypted", "data": "encrypted"}]}, True),
    ({"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {"name": "lookup"}}]}, False),
    ({"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}, True),
])
def test_public_claude_typed_readable_opaque_and_tool_snapshots(monkeypatch, isolated, asynchronous, delta, early):
    observed = []
    # Finish any opened tool call without changing its original name or arguments.
    tail = {"content": "OK"}
    if "tool_calls" in delta:
        tail["tool_calls"] = [{"index": 0, "function": {"arguments": "" if early else "{}"}}]
    def read(index):
        if index == 1:
            assert ("receiving" in phases(observed)) is early
    (msg, _), sent, *_ = exchange(monkeypatch, isolated,
        [sse(chunk(delta), done=False), sse(chunk(tail, "tool_calls" if "tool_calls" in delta else "stop"))],
        asynchronous=asynchronous, observer=observed.append, on_read=read,
        model="openai-compatible::claude-sonnet-5.5")
    assert msg["content"] == "OK" and len(sent) == 1
    assert phases(observed) == ["preparing", "awaiting_response", "receiving", "response_received"]


def test_actual_physical_owner_resets_before_next_admission(monkeypatch):
    from ouroboros import llm_attempt
    rows, sends = [], []
    primary = RuntimeError("primary-failure")
    def execute(request, send, before_dispatch=None):
        assert phases(rows)[-1] == "preparing", "next admission cannot retain Receiving"
        return send()
    monkeypatch.setattr(llm_attempt, "execute_physical_attempt", execute)
    monkeypatch.setattr(llm_attempt, "require_physical_dispatch_window", lambda: None)
    monkeypatch.setattr(llm_attempt, "_retain_received_candidate", lambda request, value: value)
    @activity.model_activity_scoped
    def invoke(model_operation_observer=None):
        def send():
            sends.append(True)
            activity.observe_model_phase("awaiting_response")  # injected executor has no real sender wrapper
            activity.observe_model_output(True)
            if len(sends) == 1:
                raise primary
            return "original-response"
        with pytest.raises(RuntimeError) as caught:
            llm_attempt._execute_candidate(None, send, None)
        assert caught.value is primary and phases(rows)[-1] == "unknown"
        return llm_attempt._execute_candidate(None, send, None)
    assert invoke(model_operation_observer=rows.append) == "original-response"
    assert len(sends) == 2
    assert phases(rows) == ["preparing", "awaiting_response", "receiving", "unknown",
                            "preparing", "awaiting_response", "receiving", "response_received"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_capacity_wait_does_not_publish_sending(monkeypatch, tmp_path, asynchronous):
    """The actual sender waits for a slot before claiming dispatch; no provider needed."""
    import threading
    import time
    from ouroboros import config, llm_attempt, model_concurrency as mc

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", "10")
    monkeypatch.setattr(llm_attempt, "require_physical_dispatch_window", lambda: None)
    monkeypatch.setattr(llm_attempt, "_retain_received_candidate", lambda request, value: value)
    route = {"provider": "fixture", "base_url": "https://provider.invalid/v1",
             "api_key": "offline", "usage_model": "capacity-fixture"}
    rows, sent, errors = [], [], []
    mc.reset_for_tests()
    def queued():
        return bool(list((tmp_path / "state" / "model_concurrency").glob("*/*.ticket")))
    def send():
        assert phases(rows)[-1] == "awaiting_response"
        sent.append(True)
        activity.observe_model_output(True)
        return "accepted"
    if asynchronous:
        async def execute(request, sender, before_dispatch=None):
            return await sender()
        monkeypatch.setattr(llm_attempt, "execute_physical_attempt_async", execute)
        async def async_send():
            return send()
        @activity.model_activity_scoped
        async def invoke(model_operation_observer=None):
            return await llm_attempt._execute_candidate_async(None, mc.limit_physical_send(route, async_send), None)
        async def run():
            task = None
            try:
                with mc.model_call_slot("capacity-fixture", route=route):
                    task = asyncio.create_task(invoke(model_operation_observer=rows.append))
                    end = time.monotonic() + 5
                    while not queued():
                        assert time.monotonic() < end and not task.done()
                        await asyncio.sleep(0.01)
                    assert phases(rows) == ["preparing"] and not sent
            finally:
                if task is not None:
                    assert await asyncio.wait_for(task, 5) == "accepted"
        try:
            asyncio.run(run())
        finally:
            mc.reset_for_tests()
    else:
        def execute(request, sender, before_dispatch=None):
            return sender()
        monkeypatch.setattr(llm_attempt, "execute_physical_attempt", execute)
        @activity.model_activity_scoped
        def invoke(model_operation_observer=None):
            return llm_attempt._execute_candidate(None, mc.limit_physical_send(route, send), None)
        def worker():
            try:
                assert invoke(model_operation_observer=rows.append) == "accepted"
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        try:
            try:
                with mc.model_call_slot("capacity-fixture", route=route):
                    thread.start()
                    end = time.monotonic() + 5
                    while not queued():
                        assert time.monotonic() < end and thread.is_alive()
                        time.sleep(0.01)
                    assert phases(rows) == ["preparing"] and not sent
            finally:
                thread.join(5)
                assert not thread.is_alive()
        finally:
            mc.reset_for_tests()
    assert not errors and sent == [True]
    assert phases(rows) == ["preparing", "awaiting_response", "receiving", "response_received"]
