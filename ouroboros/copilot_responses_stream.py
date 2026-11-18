"""Native Responses lifecycle receipt, not a second output/delta converter.

Only the terminal response is usable. Raw decoded chunks and local extent facts
are retained privately by the codec even when iteration or framing fails.
"""
from __future__ import annotations

import json
from functools import partial

from ouroboros.llm_stream import _SSEFrames


class ResponsesStream:
    def __init__(self):
        self.frames = _SSEFrames()
        self.raw = []
        self.terminal = None
        self.observed_output = False
        self.text = []
        self.facts = {"terminal_received": False, "decoded_bytes_received": 0}

    def feed(self, chunk, *, final=False):
        from ouroboros.copilot_responses import CopilotPendingResponse, CopilotRejectedResponse

        self.raw.append(chunk)
        self.facts["decoded_bytes_received"] += len(chunk)
        try:
            for event, data in self.frames.feed(chunk, final=final):
                if data == "[DONE]":
                    raise CopilotPendingResponse({"stream": "DONE without native terminal"})
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise CopilotPendingResponse({"stream": "event is not an object"})
                kind = value.get("type") or event
                if not isinstance(kind, str):
                    raise CopilotPendingResponse({"stream": "event type is not a string"})
                if kind in {"response.completed", "response.failed", "response.incomplete", "response.cancelled", "error"}:
                    body = value.get("response")
                    if kind == "error":
                        body = {**value, "status": "failed", "error": value.get("error") or {
                            k: value[k] for k in ("code", "message", "param") if k in value}}
                        # Even an empty error frame is a received rejection, not success.
                        if not body["error"]:
                            body["error"] = {"type": "error"}
                    if not isinstance(body, dict):
                        raise CopilotRejectedResponse(value, "Copilot Responses terminal is not an object")
                    expected = kind.removeprefix("response.") if kind != "error" else "failed"
                    if body.get("status") != expected:
                        raise CopilotRejectedResponse(body, "Copilot Responses terminal status disagrees with event")
                    self.terminal = body
                    self.facts.update(terminal_received=True,
                        bytes_after_terminal_in_chunk=len(self.frames.buffer))
                    self.facts["terminal_end_byte"] = self.facts["decoded_bytes_received"] - len(self.frames.buffer)
                    output = body.get("output")
                    if self.text:
                        # Anomaly only: terminal remains authoritative, never reconstruct tools/text.
                        final_text = "".join(c.get("text", "") for item in output or [] if isinstance(item, dict)
                            and item.get("type") == "message" for c in (item["content"] if isinstance(item.get("content"), list) else [])
                            if isinstance(c, dict) and isinstance(c.get("text"), str)) if isinstance(output, list) else ""
                        self.facts["text_delta_terminal_disagreement"] = "".join(self.text) != final_text
                    return True
                if kind == "response.output_text.delta" and isinstance(value.get("delta"), str):
                    self.text.append(value["delta"])
                    self.observed_output |= bool(value["delta"])
                elif kind == "response.function_call_arguments.delta":
                    self.observed_output |= bool(value.get("delta"))
                elif kind in {"response.output_item.added", "response.output_item.done"}:
                    item = value.get("item")
                    if isinstance(item, dict):
                        if item.get("type") == "function_call":
                            self.observed_output |= bool(item.get("name") or item.get("arguments"))
                        elif item.get("type") == "message" and isinstance(item.get("content"), list):
                            self.observed_output |= any(isinstance(c, dict) and c.get("type") in ("text", "output_text")
                                and isinstance(c.get("text"), str) and bool(c["text"]) for c in item["content"])
        except (ValueError, UnicodeError) as error:
            raise CopilotPendingResponse({"stream": "malformed SSE before terminal"}) from error
        return False

    def result(self):
        from ouroboros.copilot_responses import CopilotPendingResponse
        if self.terminal is None:
            raise CopilotPendingResponse({"stream": "EOF without native terminal"})
        return json.dumps(self.terminal).encode("utf-8")


def _retain(response, state):
    from ouroboros.llm_observability import retain_http_observation
    try:
        retain_http_observation("native_response", response.request, response=response,
                                body=b"".join(state.raw), stream_facts=state.facts)
    except Exception:
        pass  # Evidence/log sinks cannot replace a received result or primary fault.


def read_stream(response):
    from ouroboros.response_cleanup import close_response
    state = ResponsesStream()
    try:
        for chunk in response.iter_bytes():
            if state.feed(chunk):
                break
        if state.terminal is None:
            state.feed(b"", final=True)
        return state.result(), state.observed_output
    finally:
        _retain(response, state)
        close_response(response)


async def read_stream_async(response):
    from ouroboros._usage_wait import postresponse_off_loop
    from ouroboros.response_cleanup import close_response_async
    state = ResponsesStream()
    try:
        async for chunk in response.aiter_bytes():
            if state.feed(chunk):
                break
        if state.terminal is None:
            state.feed(b"", final=True)
        return state.result(), state.observed_output
    finally:
        try:
            await postresponse_off_loop(partial(_retain, response, state), retain_on_cancel=None)
        finally:
            await close_response_async(response)
