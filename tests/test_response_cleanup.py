"""Decoded response cleanup fidelity through real SDK, mocked HTTP and accounting."""
import asyncio
import gzip
import json
from pathlib import Path

import pytest

from ouroboros import net_transport as nt, request_wire_recovery as recovery
from ouroboros.llm import LLMClient
from ouroboros.observability import read_blob_ref
from tests._usage_store_testing import ledger_rows
from tests.test_copilot_option_fidelity import option_wire  # noqa: F401


@pytest.mark.parametrize("mode", ["translated", "gpt-chat", "claude-chat"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("fault", ["control", "terminal-close", "timeout-close", "timeout-only"])
@pytest.mark.parametrize("construction", ["cached", "no-proxy", "sdk-default"])
def test_public_decoded_response_keeps_result_or_primary_fault(
    option_wire, monkeypatch, mode, asynchronous, stream, compressed, fault, construction,
):
    invoke, sent, _, root, task = option_wire
    hm = nt._openai_httpx_module()
    if construction == "sdk-default":
        monkeypatch.setattr(nt, "keepalive_http_client", lambda *args, **kwargs: None)
    resolver = LLMClient._resolve_remote_target
    if mode != "translated":
        def target(self, model):
            value = resolver(self, model)
            value.update(base_url="https://example.invalid/v1",
                         resolved_model="claude-fixture" if mode == "claude-chat" else "gpt-fixture")
            value["usage_model"] = "openai-compatible/" + value["resolved_model"]
            return value
        monkeypatch.setattr(LLMClient, "_resolve_remote_target", target)
        monkeypatch.setattr(recovery, "observe_wire_semantics", lambda **kwargs: None)
    model = "gpt-6.1-sol" if mode == "translated" else "claude-fixture" if mode == "claude-chat" else "gpt-fixture"
    usage = dict(prompt_tokens=12, completion_tokens=4, total_tokens=16)
    message = dict(role="assistant", content="Aβ😀Z")
    if mode == "translated":
        body = dict(id="native", model=model, created_at=1, status="completed",
                    output=[dict(type="message", role="assistant", status="completed",
                                 content=[dict(type="output_text", text=message["content"])])],
                    usage=dict(input_tokens=12, output_tokens=4, total_tokens=16))
    else:
        body = dict(id="native", model=model, created=1, object="chat.completion",
                    choices=[dict(index=0, message=message, finish_reason="stop")], usage=usage)
    ctype = "application/json"
    raw = json.dumps(body).encode("utf-8")
    if mode != "translated" and stream:
        body["object"] = "chat.completion.chunk"
        body["choices"][0]["delta"] = body["choices"][0].pop("message")
        raw = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode("utf-8")
        ctype = "text/event-stream"
    if compressed:
        raw = gzip.compress(raw)
    closed = []
    def pieces():
        if fault.startswith("timeout"):
            raise hm.ReadTimeout("primary read timeout")
        for i in range(0, len(raw), 13):
            yield raw[i:i + 13]
    def close():
        closed.append(True)
        if fault.endswith("close"):
            raise RuntimeError("secondary close failure")
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield from pieces()
        def close(self):
            close()
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            for item in pieces():
                yield item
        async def aclose(self):
            close()
    headers = {"content-type": ctype, **({"content-encoding": "gzip"} if compressed else {})}
    def handle(self, request):
        sent.append(dict(path=request.url.path, body=json.loads(request.content)))
        return hm.Response(200, headers=headers, stream=Sync())
    async def ahandle(self, request):
        sent.append(dict(path=request.url.path, body=json.loads(request.content)))
        return hm.Response(200, headers=headers, stream=Async())
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    if fault.startswith("timeout"):
        with pytest.raises(BaseException) as error:
            invoke(asynchronous=asynchronous, stream=stream, no_proxy=construction == "no-proxy")
        chain = []
        cursor = error.value
        while cursor is not None and cursor not in chain:
            chain.append(cursor)
            cursor = cursor.__cause__ or cursor.__context__
        assert any(isinstance(item, hm.ReadTimeout) for item in chain)
        assert not isinstance(error.value, RuntimeError)
    else:
        result, returned = invoke(asynchronous=asynchronous, stream=stream, no_proxy=construction == "no-proxy")
        assert result["content"] == message["content"]
        assert returned["prompt_tokens"] == 12 and returned["completion_tokens"] == 4
    rows = [row for row in ledger_rows(root) if row.get("task_id") == task]
    assert len(sent) == len(closed) == len(rows) == 1
    assert rows[0]["state"] == ("unresolved" if fault.startswith("timeout") else "settled")
    cleanup = []
    for path in (Path(root) / "observability" / "calls" / task).glob("*_cleanup*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        payload = read_blob_ref(root, manifest["full_payload_ref"])
        cleanup.append(payload)
    if fault.endswith("close"):
        assert len(cleanup) == 1
        assert cleanup[0]["connection_release"] == "unconfirmed"
        assert cleanup[0]["error_type"] == "RuntimeError"
        assert cleanup[0]["attempt_id"] == rows[0]["attempt_id"]
    else:
        assert cleanup == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("control", [KeyboardInterrupt, asyncio.CancelledError, GeneratorExit])
def test_cleanup_controls_propagate_and_double_close_is_not_retried(monkeypatch, asynchronous, control):
    from ouroboros.response_cleanup import protect_httpx_client
    hm = nt._openai_httpx_module()
    calls = []
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield b"ok"
        def close(self):
            calls.append(True)
            raise control("control")
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"
        async def aclose(self):
            calls.append(True)
            raise control("control")
    if asynchronous:
        async def run():
            async with hm.AsyncClient(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Async()))) as client:
                protect_httpx_client(client, asynchronous=True)
                response = await client.send(client.build_request("GET", "https://example.invalid"), stream=True)
                with pytest.raises(control):
                    await response.aclose()
                await response.aclose()
        asyncio.run(run())
    else:
        with hm.Client(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Sync()))) as client:
            protect_httpx_client(client)
            response = client.send(client.build_request("GET", "https://example.invalid"), stream=True)
            with pytest.raises(control):
                response.close()
            response.close()
    assert calls == [True]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cleanup_recording_and_logging_failures_cannot_replace_result(monkeypatch, asynchronous):
    from ouroboros import response_cleanup as cleanup, observability
    hm = nt._openai_httpx_module()
    def fail(*args, **kwargs):
        raise OSError("evidence sink failed")
    monkeypatch.setattr(observability, "persist_call", fail)
    monkeypatch.setattr(cleanup.logging.Logger, "warning", fail)
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield gzip.compress(b"decoded")
        close = fail
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(b"decoded")
        async def aclose(self):
            fail()
    # A constructed owner makes the persister path execute even without a paid call.
    from types import SimpleNamespace
    monkeypatch.setattr(cleanup, "_recording_owner", lambda: (SimpleNamespace(attempt_id="fixture"), Path("."), "fixture"))
    if asynchronous:
        async def run():
            async with hm.AsyncClient(transport=hm.MockTransport(lambda req: hm.Response(200, headers={"content-encoding": "gzip"}, stream=Async()))) as client:
                cleanup.protect_httpx_client(client, asynchronous=True)
                cleanup.protect_httpx_client(client, asynchronous=True)
                assert len(client.event_hooks["response"]) == 1
                response = await client.get("https://example.invalid")
                assert response.content == b"decoded"
        asyncio.run(run())
    else:
        with hm.Client(transport=hm.MockTransport(lambda req: hm.Response(200, headers={"content-encoding": "gzip"}, stream=Sync()))) as client:
            cleanup.protect_httpx_client(client)
            cleanup.protect_httpx_client(client)
            assert len(client.event_hooks["response"]) == 1
            assert client.get("https://example.invalid").content == b"decoded"


@pytest.mark.parametrize("fault", ["control", "terminal-close", "read-error", "read-error-close"])
def test_native_requests_assembly_preserves_primary_and_terminal(monkeypatch, fault):
    import requests
    from ouroboros.llm_stream import consume_stream
    from tests.test_transport_b_stream_deadlines import native_events, sse
    events, expected = native_events()
    calls = []
    class Raw:
        def stream(self, *args, **kwargs):
            assert kwargs.get("decode_content") is True
            if fault.startswith("read-error"):
                raise requests.exceptions.ChunkedEncodingError("primary")
            yield sse(*events, done=False)
        def close(self):
            pass
        def release_conn(self):
            calls.append(True)
            if fault.endswith("close"):
                raise OSError("secondary close")
    response = requests.Response()
    response.status_code = 200
    response.raw = Raw()
    if fault.startswith("read-error"):
        with pytest.raises(requests.exceptions.ChunkedEncodingError, match="primary"):
            consume_stream(response, native=True)
    else:
        body = consume_stream(response, native=True).model_dump()
        body.pop("_stream_receipt")
        assert body == expected
    assert calls == [True]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("close_fault", [False, True])
def test_missing_attribution_cannot_destroy_received_body(monkeypatch, asynchronous, close_fault):
    from ouroboros import response_cleanup as cleanup, usage_accounting as ua
    hm = nt._openai_httpx_module()
    def unavailable():
        raise OSError("attribution unavailable")
    monkeypatch.setattr(ua, "last_physical_attempt_capture", unavailable)
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield b"complete"
        def close(self):
            if close_fault:
                raise OSError("close")
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            yield b"complete"
        async def aclose(self):
            if close_fault:
                raise OSError("close")
    if asynchronous:
        async def run():
            async with hm.AsyncClient(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Async()))) as client:
                cleanup.protect_httpx_client(client, asynchronous=True)
                assert (await client.get("https://example.invalid")).content == b"complete"
        asyncio.run(run())
    else:
        with hm.Client(transport=hm.MockTransport(lambda req: hm.Response(200, stream=Sync()))) as client:
            cleanup.protect_httpx_client(client)
            assert client.get("https://example.invalid").content == b"complete"


@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_sdk_status_survives_cleanup_without_resend(monkeypatch, status, asynchronous):
    import openai
    hm = nt._openai_httpx_module()
    sent = []
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield b'{"error":{"message":"original status","type":"invalid_request_error"}}'
        def close(self):
            raise OSError("secondary")
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"message":"original status","type":"invalid_request_error"}}'
        async def aclose(self):
            raise OSError("secondary")
    def handle(self, request):
        sent.append(request.url.path)
        return hm.Response(status, headers={"content-type": "application/json"}, stream=Sync())
    async def ahandle(self, request):
        sent.append(request.url.path)
        return hm.Response(status, headers={"content-type": "application/json"}, stream=Async())
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    target = dict(api_key="fixture", base_url="https://example.invalid/v1")
    error_type = openai.BadRequestError if status == 400 else openai.RateLimitError
    if asynchronous:
        async def run():
            client, _ = nt.make_no_proxy_async_client(target, 1)
            try:
                with pytest.raises(error_type) as error:
                    await client.chat.completions.create(model="unlisted-model", messages=[])
                assert error.value.status_code == status
            finally:
                await client.close()
        asyncio.run(run())
    else:
        client = nt.web_search_openai_client(api_key="fixture", base_url=target["base_url"], timeout=1)
        try:
            with pytest.raises(error_type) as error:
                client.chat.completions.create(model="unlisted-model", messages=[])
            assert error.value.status_code == status
        finally:
            client.close()
    assert sent == ["/v1/chat/completions"]


@pytest.mark.parametrize("throwing_logger", [False, True])
def test_cancel_during_cleanup_recording_is_not_paid_response_retention(monkeypatch, throwing_logger):
    import threading
    from unittest.mock import Mock
    from ouroboros import response_cleanup as cleanup, _usage_wait as waits
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    logger = Mock()
    if throwing_logger:
        logger.exception.side_effect = OSError("secondary logging failure")
    monkeypatch.setattr(waits, "log", logger)

    def recording(error, owner):
        started.set()
        assert release.wait(5)
        finished.set()
        # The real cleanup recorder returns no received-answer object.
        return None

    monkeypatch.setattr(cleanup, "_record_failure", recording)

    async def failing_close():
        raise OSError("cleanup failed")

    async def run():
        task = asyncio.create_task(cleanup._aclose(failing_close, (None, None, "fixture")))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)  # Deliver the caller cancellation while recording is held.
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
            logger.exception.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("throwing_logger", [False, True])
def test_received_answer_retention_failure_cannot_replace_cancellation(monkeypatch, throwing_logger):
    import threading
    from unittest.mock import Mock
    from ouroboros import _usage_wait as waits
    started, release = threading.Event(), threading.Event()
    logger = Mock()
    if throwing_logger:
        logger.exception.side_effect = OSError("logging failed")
    monkeypatch.setattr(waits, "log", logger)
    answer = {"answer": "received"}

    def received():
        started.set()
        assert release.wait(5)
        return answer

    def retention(value, capture):
        assert value is answer
        raise OSError("retention failed")

    async def run():
        task = asyncio.create_task(waits.postresponse_off_loop(received, retain_on_cancel=retention))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert error.value.response is answer
            assert error.value.response_retention_error == "OSError"
            assert logger.exception.call_count == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("throwing_logger", [False, True])
def test_completion_won_model_cancel_keeps_control_after_retention_failure(monkeypatch, throwing_logger):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ouroboros import _usage_wait as waits, owner_pause
    logger = Mock()
    if throwing_logger:
        logger.exception.side_effect = OSError("logging failed")
    monkeypatch.setattr(waits, "log", logger)
    answer = {"answer": "completion won"}
    sent = []

    def submit(reservation, consumer, function):
        caller = asyncio.current_task()
        future = consumer(lambda action, *args: action(*args), function)
        future.add_done_callback(lambda done: caller.cancel())
        sent.append(future)
        return future

    monkeypatch.setattr(owner_pause, "submit_model", submit)

    async def complete():
        return answer

    def retention(value, capture):
        assert value is answer
        raise OSError("retention failed")

    async def run():
        with pytest.raises(asyncio.CancelledError) as error:
            await waits.model_send_async(SimpleNamespace(attempt_id="fixture"), complete,
                                         retain_on_cancel=retention)
        assert error.value.response is answer
        assert error.value.response_retention_error == "OSError"
        assert len(sent) == 1 and sent[0].done() and not sent[0].cancelled()
        assert logger.exception.call_count == 1

    asyncio.run(run())
