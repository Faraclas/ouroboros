"""Evidence sinks and native terminal shapes through real SDK, offline HTTP only."""
import asyncio
import copy
import gzip
import json
import logging
from types import SimpleNamespace

import openai
import pytest

from ouroboros import copilot_responses as codec, llm_observability as obs
from ouroboros import net_transport as nt, request_wire_recovery as recovery
from ouroboros.llm import LLMClient
from ouroboros.observability import read_blob_ref
from tests._usage_store_testing import ledger_rows
from tests.test_copilot_option_fidelity import option_wire  # noqa: F401


class ThrowingHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("injected diagnostic handler failure")


def failing_sink(monkeypatch):
    original = obs.persist_call
    def persist(*args, **kwargs):
        if kwargs.get("call_type") in {"physical_response", "physical_http"}:
            raise OSError("injected evidence persistence failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(obs, "persist_call", persist)
    logger = logging.getLogger(obs.__name__)
    monkeypatch.setattr(logger, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)


@pytest.mark.parametrize("mode", ["responses", "gpt-chat", "claude-chat"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fault", ["reply", "http400", "read-timeout"])
def test_public_evidence_sinks_cannot_replace_result_or_primary(
    option_wire, monkeypatch, mode, asynchronous, stream, fault,
):
    invoke, sent, _, root, task = option_wire
    hm = nt._openai_httpx_module()
    resolver = LLMClient._resolve_remote_target
    if mode != "responses":
        def target(self, model):
            value = resolver(self, model)
            value.update(resolved_model=mode, usage_model="openai-compatible/" + mode,
                         base_url="https://example.invalid/v1")
            return value
        monkeypatch.setattr(LLMClient, "_resolve_remote_target", target)
        monkeypatch.setattr(recovery, "observe_wire_semantics", lambda **kwargs: None)
    failing_sink(monkeypatch)
    message = {"role": "assistant", "content": "Aβ😀Z"}
    body = dict(id="fixture", model=mode, created=1, object="chat.completion",
                choices=[dict(index=0, message=message, finish_reason="stop")],
                usage=dict(prompt_tokens=12, completion_tokens=4, total_tokens=16))
    if mode == "responses":
        body = dict(id="fixture", model="gpt-6.1-sol", status="completed", created_at=1,
                    output=[dict(type="message", content=[dict(type="output_text", text=message["content"])])],
                    usage=dict(input_tokens=12, output_tokens=4, total_tokens=16))
    content_type = "application/json"
    if mode != "responses" and stream:
        body["object"] = "chat.completion.chunk"
        body["choices"][0]["delta"] = body["choices"][0].pop("message")
        raw = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode("utf-8")
        content_type = "text/event-stream"
    else:
        raw = json.dumps(body).encode("utf-8")
    raw = gzip.compress(raw)
    def pieces():
        if fault == "read-timeout":
            raise hm.ReadTimeout("primary read fault")
        yield raw
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield from pieces()
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            for piece in pieces():
                yield piece
    def response(request, byte_stream):
        sent.append(dict(path=request.url.path, body=json.loads(request.content)))
        if fault == "http400":
            return hm.Response(400, json={"error": {"code": "invalid_request_error", "message": "fixture"}})
        return hm.Response(200, headers={"content-type": content_type, "content-encoding": "gzip"},
                           stream=byte_stream)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", lambda self, request: response(request, Sync()))
    async def handle(self, request):
        return response(request, Async())
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", handle)
    if fault == "reply":
        result, usage = invoke(asynchronous=asynchronous, stream=stream)
        assert result["content"] == "Aβ😀Z"
        assert usage["prompt_tokens"] == 12
    elif fault == "http400":
        with pytest.raises(openai.BadRequestError) as error:
            invoke(asynchronous=asynchronous, stream=stream)
        assert error.value.status_code == 400
        assert error.value.body == {"code": "invalid_request_error", "message": "fixture"}
    else:
        with pytest.raises(BaseException) as error:
            invoke(asynchronous=asynchronous, stream=stream)
        chain, current = [], error.value
        while current is not None and current not in chain:
            chain.append(current)
            current = current.__cause__ or current.__context__
        assert any(isinstance(value, hm.ReadTimeout) for value in chain)
        assert not isinstance(error.value, RuntimeError)
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert len(sent) == len(rows) == 1
    # HTTP400 carries no usage in this fixture: keep unknown accounting, not zero.
    assert rows[0]["state"] == ("settled" if fault == "reply" else "unresolved")


BAD_OUTPUTS = [{"not": "a-list"}, "not-a-list", 7, False, ["not-an-item"],
               [{"type": []}], [{"type": "message", "content": [{"type": []}]}]]


@pytest.mark.parametrize("output", BAD_OUTPUTS)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_public_malformed_terminal_is_rejected_with_original_body(
    option_wire, monkeypatch, output, asynchronous, stream,
):
    invoke, sent, _, root, task = option_wire
    hm = nt._openai_httpx_module()
    body = dict(id="malformed", model="gpt-6.1-sol", status="completed", output=output,
                usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16})
    def handle(self, request):
        sent.append(dict(path=request.url.path, body=json.loads(request.content)))
        return hm.Response(200, json=body)
    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    with pytest.raises(codec.CopilotRejectedResponse) as error:
        invoke(asynchronous=asynchronous, stream=stream)
    assert error.value.body == body
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert len(sent) == len(rows) == 1 and rows[0]["state"] == "settled"
    retained = []
    for path in (root / "observability" / "calls" / task).glob("*_http_native_response_*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        retained.append(read_blob_ref(root, manifest["full_payload_ref"]))
    assert len(retained) == 1 and json.loads(retained[0]["decoded_body"]) == body


@pytest.mark.parametrize("output", [None, [], [{"type": "future-output", "payload": {}}]])
def test_converter_optional_empty_and_future_output_survive(output):
    body = dict(status="completed", output=output, usage={"input_tokens": None})
    before = copy.deepcopy(body)
    result = codec._copilot_responses_to_chat_completion(body, "gpt-fixture")
    assert result["choices"][0]["message"]["content"] == ""
    assert result["choices"][0]["finish_reason"] == "stop" and body == before


@pytest.mark.parametrize("entry", ["bad", 1, False, ["bad"]])
def test_converter_malformed_consumed_content_is_rejected(entry):
    body = dict(status="completed", output=[dict(type="message", content=entry)])
    with pytest.raises(codec.CopilotRejectedResponse) as error:
        codec._copilot_responses_to_chat_completion(body, "gpt-fixture")
    assert error.value.body == body


@pytest.mark.parametrize("sink", ["public", "received", "projection", "attachment", "http"])
def test_shared_logging_failure_is_nonfatal_and_control_is_not_swallowed(monkeypatch, tmp_path, sink):
    failing_sink(monkeypatch)
    monkeypatch.setattr(obs, "http_projection", lambda request: (_ for _ in ()).throw(ValueError("projection")))
    class NoAttachment:
        __slots__ = ()
    def invoke():
        if sink == "public":
            return obs.persist_observed_call(tmp_path, payload={}, writer=lambda *a, **k: (_ for _ in ()).throw(OSError("write")))
        if sink == "received":
            return obs.retain_received_response(SimpleNamespace(model="other", provider="other"), {})
        if sink == "http":
            return obs.retain_http_observation("request_hook", object())
        with obs.http_observation_scope():
            if sink == "projection":
                return obs.note_http_projection(object())
            obs._HTTP_OBSERVATIONS.get().append({"boundary": "fixture"})
            return obs.bind_http_observations(NoAttachment())
    value = invoke()
    assert value == {} if sink in {"public", "received", "http"} else value is None if sink == "projection" else isinstance(value, NoAttachment)
    class ControlHandler(logging.Handler):
        def emit(self, record):
            raise KeyboardInterrupt("control")
    monkeypatch.setattr(logging.getLogger(obs.__name__), "handlers", [ControlHandler()])
    with pytest.raises(KeyboardInterrupt):
        invoke()


@pytest.mark.parametrize("field,value", [("summary", "bad"), ("summary", 7), ("text", {}), ("text", 7)])
def test_converter_malformed_nested_fields_are_typed(field, value):
    item = dict(type="reasoning", summary=value) if field == "summary" else dict(type="message", content=[dict(type="output_text", text=value)])
    body = dict(status="completed", output=[item])
    with pytest.raises(codec.CopilotRejectedResponse) as error:
        codec._copilot_responses_to_chat_completion(body, "gpt-fixture")
    assert error.value.body == body


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["responses", "claude-chat"])
def test_public_accounting_gap_and_handler_failure_keep_received_body(option_wire, monkeypatch, asynchronous, mode):
    from ouroboros import usage_accounting as ua
    invoke, sent, _, root, task = option_wire
    hm = nt._openai_httpx_module()
    if mode == "claude-chat":
        resolver = LLMClient._resolve_remote_target
        def target(self, model):
            value = resolver(self, model)
            value.update(base_url="https://example.invalid/v1", resolved_model="claude-fixture", usage_model="openai-compatible/claude-fixture")
            return value
        monkeypatch.setattr(LLMClient, "_resolve_remote_target", target)
        monkeypatch.setattr(recovery, "observe_wire_semantics", lambda **kwargs: None)
        def handle(self, request):
            sent.append(dict(path=request.url.path, body=json.loads(request.content)))
            return hm.Response(200, json=dict(id="fixture", model="claude-fixture", created=1, object="chat.completion", choices=[dict(index=0, message=dict(role="assistant", content='{"ok":true}'), finish_reason="stop")], usage=dict(prompt_tokens=12, completion_tokens=4)))
        async def ahandle(self, request):
            return handle(self, request)
        monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
        monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    def fail(*a, **k):
        raise OSError("injected settlement write failure")
    monkeypatch.setattr(ua, "settle_attempt", fail)
    monkeypatch.setattr(ua.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(ua.log, "propagate", False)
    monkeypatch.setattr(ua.log, "disabled", False)
    monkeypatch.setattr(ua.log, "level", logging.WARNING)
    result, usage = invoke(asynchronous=asynchronous)
    assert json.loads(result["content"]) == {"ok": True}
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert len(sent) == len(rows) == 1 and rows[0]["state"] == "unresolved"


@pytest.mark.parametrize("field,value", [("created_at", "bad"), ("created_at", {}), ("status", [])])
def test_terminal_metadata_is_typed_not_unknown_on_complete_body(field, value):
    body = dict(status="completed", output=[], created_at=1)
    body[field] = value
    with pytest.raises(codec.CopilotRejectedResponse) as error:
        codec._copilot_responses_to_chat_completion(body, "gpt-fixture")
    assert error.value.body == body


@pytest.mark.parametrize("boundary", ["request_hook", "native_response"])
def test_cancel_joins_real_failing_http_observer_without_adopting_answer(option_wire, monkeypatch, boundary):
    import threading
    from ouroboros import usage_accounting as ua
    _, sent, _, root, task_id = option_wire
    failing_sink(monkeypatch)
    original = obs.persist_call
    started, release, ended = threading.Event(), threading.Event(), threading.Event()
    def persist(*args, **kwargs):
        if "_http_" + boundary + "_" in kwargs.get("call_id", ""):
            started.set()
            assert release.wait(5), "parent did not release held observer"
            try:
                return original(*args, **kwargs)
            finally:
                ended.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(obs, "persist_call", persist)
    async def run():
        client = LLMClient()
        scope = ua.UsageScope(drive_root=root, task_id=task_id, root_task_id="offline-option-fidelity",
                              non_task_operation=True, category="verification", source="offline_mock_fixture")
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            operation = asyncio.create_task(client.chat_async(messages=[dict(role="user", content="fixture")], model="openai-compatible::gpt-6.1-sol", stream=True, max_tokens=1024, wait_for_resources=False))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                operation.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                assert ended.is_set()
            finally:
                release.set()
                for remote in client._async_remote_clients.values():
                    await remote.close()
    asyncio.run(run())
    assert len(sent) == (0 if boundary == "request_hook" else 1)


def test_gap_log_preserves_level_message_and_controls(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="fixture.accounting"):
        obs.log_observation_gap("accounting gap %s", "fixture", logger=logging.getLogger("fixture.accounting"), level=logging.ERROR)
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "fixture.accounting"] == [(logging.ERROR, "accounting gap fixture")]
    class FailingFilter(logging.Filter):
        def filter(self, record):
            raise RuntimeError("filter")
    monkeypatch.setattr(logging.getLogger(obs.__name__), "filters", [FailingFilter()])
    obs.log_observation_gap("filtered")


@pytest.mark.parametrize("itype", [[], {}])
def test_malformed_item_type_is_typed(itype):
    body = dict(status="completed", output=[dict(type=itype)])
    with pytest.raises(codec.CopilotRejectedResponse) as error:
        codec._copilot_responses_to_chat_completion(body, "gpt-fixture")
    assert error.value.body == body


def test_root_refresh_gap_does_not_throw_from_debug_handler(monkeypatch, tmp_path):
    from ouroboros import usage_accounting as ua
    monkeypatch.setattr(ua, "usage_projection", lambda *a, **k: (_ for _ in ()).throw(OSError("read")))
    monkeypatch.setattr(ua.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(ua.log, "level", logging.DEBUG)
    monkeypatch.setattr(ua.log, "propagate", False)
    assert ua.refresh_root_accounting(tmp_path, "fixture", strict=True) is None


@pytest.mark.parametrize("rung", ["cache", "router", "signature", "body", "encrypted"])
def test_recovery_warning_cannot_replace_selected_payload(monkeypatch, rung):
    from ouroboros import llm_fallback as fallback
    client = LLMClient()
    monkeypatch.setattr(fallback.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(fallback.log, "propagate", False)
    monkeypatch.setattr(fallback.log, "level", logging.WARNING)
    chosen = {"messages": [{"role": "user", "content": "unchanged"}]}
    target = {"provider": "openrouter", "supports_openrouter_extensions": True}
    monkeypatch.setattr(client, "_reroute_same_model_kwargs", lambda *a, **kw: copy.deepcopy(chosen))
    monkeypatch.setattr(fallback, "reasoning_strip_retry_allowed", lambda exc: True)
    def invoke():
        if rung == "cache":
            return client._retry_without_prompt_cache_parameter({"prompt_cache_key": "fixture"}, {"provider": "openai"}, ValueError("unsupported prompt_cache_key"))
        if rung == "router":
            response = SimpleNamespace(model_dump=lambda: {"error": {"code": 429}, "openrouter_metadata": {"attempts": [{"status": 503, "provider": "fixture"}]}})
            return client._router_reask_kwargs(target, {}, response=response)
        if rung == "signature":
            return client._openrouter_signature_retry_kwargs(target, chosen, ValueError("rejected"))
        response = SimpleNamespace(model_dump=lambda: {"error": {"code": 503 if rung == "body" else 400, "message": "encrypted content"}})
        method = client._reroute_kwargs_for_body_error if rung == "body" else client._strip_kwargs_for_encrypted_body_error
        return method(response, chosen, target)
    result = invoke()
    assert result == {} if rung == "cache" else result == {"extra_body": {"provider": {"allow_fallbacks": False, "only": ["fixture"]}}} if rung == "router" else result == chosen
    assert chosen == {"messages": [{"role": "user", "content": "unchanged"}]}
    class ControlHandler(logging.Handler):
        def emit(self, record):
            raise KeyboardInterrupt("control")
    monkeypatch.setattr(fallback.log, "handlers", [ControlHandler()])
    with pytest.raises(KeyboardInterrupt):
        invoke()


@pytest.mark.parametrize("phase", ["entered", "ended"])
def test_accounting_wait_disclosure_double_fault_keeps_owner(monkeypatch, tmp_path, phase):
    from ouroboros import _usage_wait as wait, loop_messages
    monkeypatch.setattr(loop_messages, "_emit_checkpoint_event", lambda *a, **k: (_ for _ in ()).throw(OSError("checkpoint")))
    monkeypatch.setattr(wait.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(wait.log, "propagate", False)
    monkeypatch.setattr(wait.log, "level", logging.DEBUG)
    owner = SimpleNamespace(event_queue=None, task_id="fixture", drive_root=tmp_path, task={})
    assert wait._hold(owner, phase, 0.0, "fixture") is None


@pytest.mark.parametrize("fault", ["retention", "custody"])
def test_abandoned_sender_diagnostic_cannot_skip_late_owner(monkeypatch, fault):
    from ouroboros import _usage_wait as wait, usage_accounting as ua
    monkeypatch.setattr(wait.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(wait.log, "propagate", False)
    monkeypatch.setattr(wait.log, "level", logging.ERROR)
    def fail(*a, **k):
        raise OSError("injected " + fault)
    monkeypatch.setattr(wait, "retain_unadopted", lambda *a, **k: fail if fault == "retention" else lambda *a: None)
    monkeypatch.setattr(ua, "_account_response", lambda *a: None)
    monkeypatch.setattr(ua, "last_physical_attempt_capture", lambda: None)
    seen = []
    def owner(result, error):
        seen.append((result, error))
        if fault == "custody":
            fail()
    callback = wait.late_settler(SimpleNamespace(attempt_id="fixture"), None, None, None, None, owner)
    result = {"content": "not adopted"}
    callback(result=result)
    assert seen == [(result, None)]


@pytest.mark.parametrize("fault", ["retention", "custody"])
def test_async_abandoned_sender_diagnostic_keeps_late_owner(monkeypatch, fault):
    from ouroboros import _usage_wait as wait, owner_pause
    monkeypatch.setattr(wait.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(wait.log, "propagate", False)
    monkeypatch.setattr(wait.log, "level", logging.ERROR)
    monkeypatch.setattr(wait, "_receiver_paused", lambda reservation: True)
    monkeypatch.setattr(wait, "ABANDON_POLL_SEC", 0.001)
    monkeypatch.setattr(owner_pause, "submit_model", lambda reservation, run, fn: asyncio.create_task(fn()))
    async def presend(fn, **kw):
        return fn()
    monkeypatch.setattr(wait, "retry_presend_async", presend)
    monkeypatch.setattr(wait, "_abandoned", lambda reservation, future: RuntimeError("receiver left"))
    def fail(*a):
        raise OSError("injected " + fault)
    async def run():
        release = asyncio.Event()
        seen = []
        async def complete():
            await release.wait()
            return {"content": "not adopted"}
        def owner(result, error):
            seen.append((result, error))
            if fault == "custody":
                fail()
        with wait.receiver_abandonable():
            try:
                with pytest.raises(RuntimeError, match="receiver left"):
                    await wait.model_send_async(None, complete, retain_on_cancel=None, retain_on_abandon=fail if fault == "retention" else lambda *a: None, late_owner=owner)
                senders = tuple(wait._ABANDONED_SENDS)
                assert len(senders) == 1
                release.set()
                assert await senders[0] == {"content": "not adopted"}
                assert seen == [({"content": "not adopted"}, None)]
            finally:
                release.set()
    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pre_dispatch_release_diagnostic_keeps_primary_and_unresolved(option_wire, monkeypatch, asynchronous):
    from ouroboros import transport_custody as custody, usage_accounting as ua
    invoke, sent, _, root, task = option_wire
    hm = nt._openai_httpx_module()
    import httpx
    primary = httpx.ConnectError("pre-dispatch connection failure")
    def handle(self, request):
        raise primary
    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    transition = ua._transition
    def release_fails(reservation, state, **kwargs):
        if state == "released":
            raise OSError("release write failed")
        return transition(reservation, state, **kwargs)
    monkeypatch.setattr(ua, "_transition", release_fails)
    monkeypatch.setattr(custody.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(custody.log, "propagate", False)
    monkeypatch.setattr(custody.log, "level", logging.ERROR)
    with pytest.raises(openai.APIConnectionError) as error:
        invoke(asynchronous=asynchronous)
    assert error.value.__cause__ is primary
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert not sent and len(rows) == 1 and rows[0]["state"] == "unresolved"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_settlement_and_unresolved_diagnostic_double_fault_keeps_paid_body(option_wire, monkeypatch, asynchronous):
    from ouroboros import usage_accounting as ua
    invoke, sent, _, root, task = option_wire
    def fail(*args, **kwargs):
        raise OSError("post-response store unavailable")
    monkeypatch.setattr(ua, "settle_attempt", fail)
    monkeypatch.setattr(ua, "mark_unresolved", fail)
    monkeypatch.setattr(ua.log, "handlers", [ThrowingHandler()])
    monkeypatch.setattr(ua.log, "propagate", False)
    monkeypatch.setattr(ua.log, "level", logging.ERROR)
    result, _ = invoke(asynchronous=asynchronous)
    assert json.loads(result["content"]) == {"ok": True}
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert len(sent) == len(rows) == 1 and rows[0]["state"] == "dispatched"
    assert ua.last_physical_attempt_capture().state == "dispatched"
    assert ua.usage_projection(root)["cost_final"] is False
