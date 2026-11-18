"""Native Responses SSE at the actual SDK/physical attempt boundary; no sockets."""
import asyncio
import copy
import gzip
import json

import openai
import pytest

from ouroboros import net_transport as nt, usage_accounting as ua
from ouroboros.llm import LLMClient
from tests._usage_store_testing import ledger_rows
from tests.test_transport_b_stream_deadlines import isolated as isolated
from tests.test_copilot_response_fidelity import native, message, call
from tests.test_copilot_native_custody import native_turn

HOST = "https://api.business.githubcopilot.com"
MODEL = "openai-compatible::gpt-6.1-sol"


def event(kind, **fields):
    return ("data: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n").encode()


def terminal(body):
    return event("response." + body["status"], response=body)


def exchange(monkeypatch, root, chunks, *, asynchronous=False, stream=True, fault=None,
             close_fault=False, compressed=False, again=False, media="text/event-stream", status=200,
             observer=None, on_read=None, model=MODEL, base_url=HOST):
    hm = nt._openai_httpx_module()
    sent, closed, reads = [], [], []
    async_fault = isinstance(fault, asyncio.CancelledError)
    data = gzip.compress(b"".join(chunks)) if compressed else None
    parts = [data[:5], data[5:]] if compressed else chunks

    def generate():
        for index, chunk in enumerate(parts):
            if on_read:
                on_read(index)
            reads.append(len(chunk))
            yield chunk
        if fault is not None and not async_fault:
            raise fault
    class Sync(hm.SyncByteStream):
        def __iter__(self):
            yield from generate()
        def close(self):
            closed.append(True)
            if close_fault:
                raise RuntimeError("close-sentinel")
    class Async(hm.AsyncByteStream):
        async def __aiter__(self):
            for chunk in generate():
                yield chunk
            if async_fault:
                raise fault
        async def aclose(self):
            closed.append(True)
            if close_fault:
                raise RuntimeError("close-sentinel")
    def respond(request):
        sent.append(request)
        if again and len(sent) > 1:
            return hm.Response(200, json=native(output=[message("NEXT")]))
        headers = {"content-type": media, "x-request-id": "native-stream-id"}
        if compressed:
            headers["content-encoding"] = "gzip"
        return hm.Response(status, stream=Async() if asynchronous else Sync(), headers=headers)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "offline-fixture")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base_url)
    http = (hm.AsyncClient if asynchronous else hm.Client)(transport=hm.MockTransport(respond))
    sdk = (openai.AsyncOpenAI if asynchronous else openai.OpenAI)(
        api_key="offline-fixture", base_url=base_url, max_retries=0, http_client=http)
    from ouroboros.response_cleanup import protect_httpx_client
    protect_httpx_client(http, asynchronous=asynchronous)  # Production attach_remote_client order.
    nt.attach_copilot_client(sdk)
    client = LLMClient()
    monkeypatch.setattr(client, "_get_async_remote_client" if asynchronous else "_get_remote_client", lambda *a, **k: sdk)
    history = [{"role": "user", "content": "fixture"}]
    async def arun():
        try:
            result = await client.chat_async(messages=history, model=model, stream=stream, max_tokens=1024,
                model_operation_observer=observer)
            if again:
                history.extend([result[0], {"role": "tool", "tool_call_id": "call_native", "content": "INERT"}])
                await client.chat_async(messages=history, model=MODEL, max_tokens=1024)
            return result
        finally:
            await sdk.close()
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id="native-sse", root_task_id="native-sse")):
        if asynchronous:
            result = asyncio.run(arun())
        else:
            try:
                result = client.chat(messages=history, model=model, stream=stream, max_tokens=1024,
                model_operation_observer=observer)
                if again:
                    history.extend([result[0], {"role": "tool", "tool_call_id": "call_native", "content": "INERT"}])
                    client.chat(messages=history, model=MODEL, max_tokens=1024)
            finally:
                sdk.close()
    return result, sent, closed, reads


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_public_stream_terminal_replay_and_no_drain(monkeypatch, isolated, asynchronous, compressed):
    body = native_turn()
    body["usage"] = {"input_tokens": 9, "output_tokens": 4, "cost": 0}
    wire = b"\xef\xbb\xbf: comment\r\n\r\n" + event("response.output_text.delta", delta="NOT FINAL") + terminal(body)
    # Reading EOF is deliberately fatal. A valid terminal must end this receive.
    (msg, usage), sent, closed, reads = exchange(monkeypatch, isolated, [wire],
        asynchronous=asynchronous, compressed=compressed, fault=RuntimeError("overdrain"), again=True)
    assert msg["content"] == "checkinglater" and msg["tool_calls"][0]["id"] == "call_native"
    assert usage["prompt_tokens"] == 9 and usage["completion_tokens"] == 4
    payload = json.loads(sent[0].content)
    assert payload["stream"] is True and sent[0].headers["accept"] == "text/event-stream"
    replay = json.loads(sent[1].content)["input"]
    assert replay[1] == body["output"][0]
    assert [v.get("phase") for v in replay if v.get("type") == "message"] == ["commentary", "final_answer"]
    assert closed and reads
    rows = ledger_rows(isolated)
    assert len(rows) == 2 and all(r["state"] == "settled" for r in rows)
    assert rows[0]["cost_usd"] == 0 and rows[0]["cost_final"] is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_json_fallback_and_nonstream_unchanged(monkeypatch, isolated, asynchronous, stream):
    body = native()
    (msg, usage), sent, *_ = exchange(monkeypatch, isolated, [json.dumps(body).encode()],
        asynchronous=asynchronous, stream=stream, media="application/json")
    assert msg["content"] == "OK" and usage["prompt_tokens"] is None
    payload = json.loads(sent[0].content)
    assert payload.get("stream") is (True if stream else None)
    assert ledger_rows(isolated)[0]["state"] == "settled"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("case", ["eof", "empty", "malformed", "failed", "error", "refusal", "length", "filter"])
def test_physical_failures_are_not_partial_tools(monkeypatch, isolated, asynchronous, case):
    prefix = event("response.output_item.added", item=call())
    body = native(output=[message("PARTIAL"), call()])
    if case == "eof": suffix = b""
    elif case == "empty": suffix = terminal(native(output=[]))
    elif case == "malformed": suffix = event("response.completed", response=[])
    elif case == "failed": suffix = terminal(native("failed", error={"code": "fixture"}))
    elif case == "error": suffix = event("error", code="fixture", message="failed", param="input")
    elif case == "refusal": suffix = terminal(native(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "NO"}]}]))
    else:
        body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens" if case == "length" else "content_filter"})
        suffix = terminal(body)
    if case == "length":
        (msg, usage), *_ = exchange(monkeypatch, isolated, [prefix, suffix], asynchronous=asynchronous)
        assert msg["content"] == "PARTIAL" and not msg.get("tool_calls")
    else:
        with pytest.raises((nt.CopilotPendingResponse, nt.CopilotRejectedResponse)):
            exchange(monkeypatch, isolated, [prefix, suffix], asynchronous=asynchronous)
    rows = ledger_rows(isolated)
    assert len(rows) == 1 and rows[0]["state"] == ("unresolved" if case == "eof" else "settled")
    assert rows[0].get("cost_usd") is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fragmentation_utf8_crlf_and_same_chunk_suffix(monkeypatch, isolated, asynchronous):
    data = terminal(native(output=[message("héllo世界")])).replace(b"\n", b"\r\n")
    chunks = [data[i:i+1] for i in range(len(data)-2)] + [data[-2:] + b"data: invalid\n\n"]
    (msg, _), *_ = exchange(monkeypatch, isolated, chunks, asynchronous=asynchronous,
                           fault=RuntimeError("must-not-drain"), close_fault=True)
    assert msg["content"] == "héllo世界"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "read", "cancel", "invalid", "eof"])
def test_partial_native_retention_and_primary_cause(monkeypatch, isolated, asynchronous, failure):
    import base64
    import hashlib
    import ouroboros.llm_observability as obs
    hm = nt._openai_httpx_module()
    received = []
    persist = obs.persist_call
    def retain(*args, **kwargs):
        result = persist(*args, **kwargs)
        if kwargs.get("call_type") == "physical_http" and kwargs["payload"]["kind"] == "native_response":
            received.append((copy.deepcopy(kwargs), result))
        return result
    monkeypatch.setattr(obs, "persist_call", retain)
    claims = {"copilot_usage": {"reported_only": 7}}
    prefix = event("response.in_progress", response={"status": "in_progress", **claims}) + event("response.output_text.delta", delta="private partial")
    fault = {"timeout": hm.ReadTimeout("primary-timeout"), "read": hm.ReadError("primary-read"),
             "cancel": asyncio.CancelledError("primary-cancel") if asynchronous else KeyboardInterrupt("primary-cancel"),
             "invalid": None, "eof": None}[failure]
    if failure == "invalid":
        prefix += b"data: \xff\n\n"
    with pytest.raises(BaseException) as caught:
        exchange(monkeypatch, isolated, [prefix], asynchronous=asynchronous, fault=fault, close_fault=True)
    if failure == "cancel":
        assert isinstance(caught.value, (asyncio.CancelledError, KeyboardInterrupt))
    elif fault is not None:
        cause = caught.value
        chain = []
        while cause is not None:
            chain.append(cause)
            cause = cause.__cause__
        assert fault in chain
    else:
        assert isinstance(caught.value, nt.CopilotPendingResponse)
    assert len(received) == 1
    entry, persisted = received[0]
    assert persisted  # Actual private CAS writer acknowledged, not just a patched observer.
    body = entry["payload"]
    raw = base64.b64decode(body["decoded_body_base64"]) if "decoded_body_base64" in body else body["decoded_body"].encode()
    assert raw == prefix and body["decoded_body_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert body["stream"]["decoded_bytes_received"] == len(prefix)
    assert body["stream"]["terminal_received"] is False
    rows = ledger_rows(isolated)
    assert len(rows) == 1 and rows[0]["state"] == "unresolved"
    assert rows[0]["provider_usage"] == {"source": "provider_response", **claims}
    assert entry["manifest"]["attempt_id"] == rows[0]["attempt_id"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_retention_and_logging_sink_failure_does_not_replace_answer_or_fault(monkeypatch, isolated, asynchronous):
    import ouroboros.llm_observability as obs
    import ouroboros.response_cleanup as cleanup
    def fail(*a, **k):
        raise RuntimeError("sink-sentinel")
    original = obs.retain_http_observation
    def retain(kind, *a, **k):
        if kind == "native_response":
            return fail()
        return original(kind, *a, **k)
    monkeypatch.setattr(obs, "retain_http_observation", retain)
    monkeypatch.setattr(cleanup.logging.getLogger(cleanup.__name__), "warning", fail)
    (msg, _), *_ = exchange(monkeypatch, isolated, [terminal(native())], asynchronous=asynchronous, close_fault=True)
    assert msg["content"] == "OK"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_non200_not_sse_error_translation(monkeypatch, isolated, asynchronous):
    with pytest.raises(openai.RateLimitError) as caught:
        exchange(monkeypatch, isolated, [b'{"error":{"code":"rate_limit","message":"fixture"}}'],
                 asynchronous=asynchronous, status=429, media="application/json")
    assert caught.value.status_code == 429
    assert caught.value.response.headers["x-request-id"] == "native-stream-id"


def test_parser_extent_future_events_multiline_and_large_output():
    from ouroboros.copilot_responses_stream import ResponsesStream
    state = ResponsesStream()
    text = "x" * 200000
    state.feed(event("future.event", arbitrary=True))
    state.feed(event("response.output_text.delta", delta="provisional"))
    wire = terminal(native(output=[message(text)]))
    wire = wire.replace(b'{"type":', b'{\ndata: "type":', 1)
    suffix = b"data: invalid\n\n"
    assert state.feed(wire + suffix)
    assert json.loads(state.result())["output"][0]["content"][0]["text"] == text
    assert state.facts["text_delta_terminal_disagreement"] is True
    assert state.facts["bytes_after_terminal_in_chunk"] == len(suffix)
    assert state.facts["terminal_end_byte"] + len(suffix) == state.facts["decoded_bytes_received"]


@pytest.mark.parametrize("output", [[], [message("")], [message(None)]])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_contradictory_semantically_empty_terminal(monkeypatch, isolated, output, asynchronous):
    with pytest.raises(nt.CopilotRejectedResponse):
        exchange(monkeypatch, isolated, [event("response.output_text.delta", delta="concrete"),
            terminal(native(output=output))], asynchronous=asynchronous)
    assert len(ledger_rows(isolated)) == 1
    assert ledger_rows(isolated)[0]["state"] == "settled"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_actually_empty_completion_is_not_globally_forbidden(monkeypatch, isolated, asynchronous):
    (msg, _), *_ = exchange(monkeypatch, isolated, [terminal(native(output=[]))], asynchronous=asynchronous)
    assert msg["content"] == "" and not msg.get("tool_calls")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_error_event_explicit_charge_and_binding(monkeypatch, isolated, asynchronous):
    with pytest.raises(nt.CopilotRejectedResponse) as caught:
        exchange(monkeypatch, isolated, [event("error", code="bad_input", message="fixture", param="input",
            usage={"input_tokens": 9, "output_tokens": 0, "cost": 0.125})], asynchronous=asynchronous)
    assert caught.value.body["error"] == {"code": "bad_input", "message": "fixture", "param": "input"}
    row, = ledger_rows(isolated)
    assert row["state"] == "settled" and row["cost_usd"] == 0.125 and row["cost_final"] is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("case", ["reasoning", "empty_item", "cr_eof"])
def test_legitimate_terminal_stream_parity(monkeypatch, isolated, asynchronous, case):
    body = native(output=[])
    prefix = b""
    if case == "reasoning":
        item = {"type": "reasoning", "id": "opaque-id", "encrypted_content": "cipher", "summary": []}
        body["output"] = [item]
        prefix = event("response.output_item.done", item=item)
    elif case == "empty_item":
        body["output"] = [message("")]
        prefix = event("response.output_item.done", item=message(""))
    suffix = terminal(body)
    if case == "cr_eof":
        suffix = suffix.replace(b"\n", b"\r")
    (msg, _), *_ = exchange(monkeypatch, isolated, [prefix, suffix], asynchronous=asynchronous)
    assert msg["content"] == "" and not msg.get("tool_calls")


@pytest.mark.parametrize("kind", [["invalid"], {"invalid": True}])
def test_invalid_event_type_is_protocol_unknown(kind):
    from ouroboros.copilot_responses_stream import ResponsesStream
    with pytest.raises(nt.CopilotPendingResponse):
        ResponsesStream().feed(event(kind))
