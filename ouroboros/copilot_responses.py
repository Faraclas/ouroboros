"""Copilot Responses codec at the HTTP client boundary, independent of socket routing.

Canonical history stays Chat-shaped. This leaf translates the existing Copilot
Responses dialect and retains separately labelled HTTP observations; proxy/TLS,
authentication, redirects and connection pools remain owned by httpx.
Invalid calls reject; length text survives without calls, usage stays nullable,
and HTTP errors retain metadata. Typed rejection may fall back but never retries;
unknown outcomes never authorize another send.
"""
from __future__ import annotations
import json
from typing import Any, Dict, Optional
from ouroboros.llm_stream import IncompleteProviderStream, RejectedProviderStream, _tool_call_problem


class CopilotRejectedResponse(RejectedProviderStream):
    """A received Responses terminal that must not become a Chat answer."""

    def __init__(self, body: Any, detail: str):
        super().__init__(detail, usage=_copilot_response_usage(body))
        self.body = body


class CopilotPendingResponse(IncompleteProviderStream):
    """A received body without a terminal generation witness; never resend it."""

    def __init__(self, body: Any):
        super().__init__("Copilot Responses body has no terminal status")
        self.body = body


def copilot_response_failure(error: BaseException) -> Optional[BaseException]:
    """Recover only our typed result from the SDK's explicit transport wrapper.

    Restore the protocol fact inside the physical send before accounting/retry.
    Ordinary connection/read errors keep their SDK classification.
    """
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        if isinstance(error, (CopilotRejectedResponse, CopilotPendingResponse)):
            return error
        seen.add(id(error))
        error = error.__cause__
    return None


def _copilot_response_usage(body: Any) -> Dict[str, Any]:
    """Translate counters without filling absent observations with measured zeros."""
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = dict(usage) if isinstance(usage, dict) else {}
    for native, chat in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                         ("input_tokens_details", "prompt_tokens_details"),
                         ("output_tokens_details", "completion_tokens_details")):
        if native in usage:
            usage[chat] = usage.pop(native)
    # A complete rejection still settles a known response, with unknown money.
    # Existing failure accounting accepts nonempty nullable counter projections.
    usage.setdefault("prompt_tokens", None)
    usage.setdefault("completion_tokens", None)
    return usage


_COPILOT_RESPONSES_MODEL_PREFIXES = ("gpt-5.3", "gpt-5.4", "gpt-5.5", "gpt-5.6", "gpt-6", "mai-code")
_COPILOT_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "xhigh",
}


def _copilot_uses_responses_api(model: str) -> bool:
    m = str(model or "").strip().lower()
    return m.startswith(_COPILOT_RESPONSES_MODEL_PREFIXES)


def _copilot_chat_to_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    import json

    model = str(payload.get("model") or "")
    out: Dict[str, Any] = {"model": model}
    input_items = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        from ouroboros.copilot_native_custody import COPILOT_WIRE_KEY
        if msg.get("role") == "assistant" and COPILOT_WIRE_KEY in msg:
            input_items.extend(msg[COPILOT_WIRE_KEY])
            continue
        role = str(msg.get("role") or "user")
        raw_content = msg.get("content")
        content_val: Any = ""
        if isinstance(raw_content, str):
            content_val = raw_content
        elif isinstance(raw_content, list):
            has_image = any(isinstance(b, dict) and b.get("type") == "image_url" for b in raw_content)
            if not has_image:
                content_val = "".join(
                    b if isinstance(b, str) else str(b.get("text") or "")
                    for b in raw_content
                    if isinstance(b, (str, dict))
                )
            else:
                blocks = []
                text_type = "output_text" if role == "assistant" else "input_text"
                for b in raw_content:
                    if isinstance(b, str):
                        blocks.append({"type": text_type, "text": b})
                    elif isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "text":
                            blocks.append({"type": text_type, "text": str(b.get("text") or "")})
                        elif btype == "image_url":
                            img = b.get("image_url")
                            url_str = img.get("url") if isinstance(img, dict) else str(img or "")
                            if url_str:
                                blocks.append({"type": "input_image", "image_url": url_str,
                                               **({"detail": img["detail"]} if isinstance(img, dict) and "detail" in img else {})})
                content_val = blocks
        if role in ("system", "developer", "user"):
            input_items.append({"role": role, "content": content_val})
        elif role == "assistant":
            if content_val:
                input_items.append({"role": "assistant", "content": content_val})
            for idx, tc in enumerate(msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                input_items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or f"call_{idx}"),
                    "name": str(fn.get("name") or ""),
                    "arguments": fn["arguments"] if isinstance(fn.get("arguments"), str) else "{}",
                })
        elif role == "tool":
            out_str = content_val if isinstance(content_val, str) else json.dumps(content_val)
            input_items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or "call_0"),
                "output": out_str,
            })
    out["input"] = input_items

    raw_tools = payload.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        resp_tools = []
        for t in raw_tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                resp_tools.append({
                    "type": "function",
                    "name": str(fn.get("name") or ""),
                    "description": str(fn.get("description") or ""),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": bool(fn.get("strict", False)),
                })
            else:
                resp_tools.append(t)
        if resp_tools:
            out["tools"] = resp_tools

    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function" and isinstance(tc.get("function"), dict):
        out["tool_choice"] = {"type": "function", "name": str(tc["function"].get("name") or "")}
    elif tc is not None:
        out["tool_choice"] = tc

    max_tok = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if isinstance(max_tok, int) and max_tok > 0:
        out["max_output_tokens"] = max(16, max_tok)

    eff = payload.get("reasoning_effort")
    if not eff and isinstance(payload.get("reasoning"), dict):
        eff = payload["reasoning"].get("effort")
    mapped_eff = _COPILOT_EFFORT_MAP.get(str(eff or "").strip().lower())
    if mapped_eff:
        out["reasoning"] = {"effort": mapped_eff}

    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type"):
        if rf["type"] == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif rf["type"] == "json_schema" and isinstance(rf.get("json_schema"), dict):
            js = rf["json_schema"]
            out["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": str(js.get("name") or "response"),
                    "schema": js.get("schema") or {},
                    "strict": bool(js.get("strict", False)),
                }
            }
            if isinstance(js.get("description"), str):
                out["text"]["format"]["description"] = js["description"]
    return out


def _copilot_responses_to_chat_completion(rjson: Dict[str, Any], requested_model: str) -> Dict[str, Any]:
    import time

    if not isinstance(rjson, dict):
        raise CopilotRejectedResponse(rjson, "Copilot Responses body is not an object")
    status = rjson.get("status")
    if status in {"failed", "cancelled"} or rjson.get("error"):
        raise CopilotRejectedResponse(rjson, "Copilot Responses generation did not complete successfully")
    if status not in {"completed", "incomplete"}:
        raise CopilotPendingResponse(rjson)
    details = rjson.get("incomplete_details")
    exhausted = status == "incomplete" and isinstance(details, dict) and details.get("reason") == "max_output_tokens"
    if status == "incomplete" and not exhausted:
        raise CopilotRejectedResponse(rjson, "Copilot Responses generation is incomplete")
    text_parts = []
    refusal_parts = []
    reasoning_parts = []
    tool_calls = []
    for idx, item in enumerate(rjson.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if not exhausted and itype in {"message", "function_call"} and item.get("status") not in (None, "completed"):
            raise CopilotRejectedResponse(rjson, "Copilot Responses output item is not complete")
        if itype == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    text_parts.append(str(c.get("text") or ""))
                elif isinstance(c, dict) and c.get("type") == "refusal":
                    refusal = str(c.get("refusal") or "")
                    refusal_parts.append(refusal)
        elif itype == "function_call" and not exhausted:
            call = {
                "id": item.get("call_id"),
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": item.get("arguments"),
                },
            }
            # Native item id is not the call/result correlation key. Preserve
            # observed strings (including empty arguments); shared execution owns
            # JSON validation and its existing empty-string no-argument policy.
            problem = _tool_call_problem(call)
            if problem:
                raise CopilotRejectedResponse(rjson, f"Copilot Responses function call {problem}")
            tool_calls.append(call)
        elif itype == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    reasoning_parts.append(str(s["text"]))

    content_text = "".join(text_parts) if text_parts else None
    if content_text is None and not tool_calls:
        content_text = ""
    message: Dict[str, Any] = {"role": "assistant", "content": content_text}
    if refusal_parts:
        # Shared consumers can mistake refusal text for a summary/answer. Keep
        # the native refusal in the rejected body's private evidence instead.
        raise CopilotRejectedResponse(rjson, "Copilot Responses generation contains a refusal")
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "\n".join(reasoning_parts)

    # Preserve truncated text/length for consumers owning truncation policy,
    # but never execute incomplete calls even when their JSON happens to parse.
    if exhausted:
        message.pop("tool_calls", None)
    finish_reason = "length" if exhausted else "tool_calls" if tool_calls else "stop"

    return {
        "id": str(rjson.get("id") or "chatcmpl-copilot-responses"),
        "object": "chat.completion",
        "created": int(rjson.get("created_at") or time.time()),
        "model": str(rjson.get("model") or requested_model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _copilot_response_usage(rjson),
    }


def _copilot_chat_completion_to_sse(chat_comp: Dict[str, Any]) -> bytes:
    import json

    choice = (chat_comp.get("choices") or [{}])[0]
    msg = dict(choice.get("message") or {"role": "assistant", "content": ""})
    if "tool_calls" in msg:
        msg["tool_calls"] = [
            {**tc, "index": idx} for idx, tc in enumerate(msg["tool_calls"])
        ]
    chunk = {
        "id": chat_comp.get("id"),
        "object": "chat.completion.chunk",
        "created": chat_comp.get("created"),
        "model": chat_comp.get("model"),
        "choices": [{
            "index": 0,
            "delta": msg,
            "finish_reason": choice.get("finish_reason") or "stop",
        }],
        "usage": chat_comp.get("usage"),
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")


def copilot_endpoint(base_url: Any) -> bool:
    """The HTTPS Copilot domain, not a URL substring or a model capability claim."""
    from urllib.parse import urlsplit
    try:
        url = urlsplit(str(base_url or ""))
        host = (url.hostname or "").lower()
        return url.scheme == "https" and (host == "githubcopilot.com" or host.endswith(".githubcopilot.com"))
    except ValueError:
        return False


def _prepare_request(request: Any, httpx_mod: Any):
    if request.method != "POST" or not copilot_endpoint(str(request.url)):
        return None
    if not request.url.path.endswith("/chat/completions"):
        return None
    try:
        payload = json.loads(request.read())
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not _copilot_uses_responses_api(payload.get("model")):
        return None
    body = json.dumps(_copilot_chat_to_responses_payload(payload)).encode("utf-8")
    path = request.url.path[:-len("/chat/completions")] + "/responses"
    headers = dict(request.headers)
    headers.update({"content-length": str(len(body)), "accept": "application/json"})
    return (httpx_mod.Request("POST", request.url.copy_with(path=path), headers=headers,
                              content=body, extensions=dict(request.extensions)),
            str(payload.get("model") or ""), bool(payload.get("stream")))


def _convert_response(resp: Any, raw: bytes, model: str, stream: bool, hm: Any):
    # The original body has already been decoded. Keep native metadata but not
    # compression/framing headers for bytes that no longer have that encoding.
    headers = [(k, v) for k, v in resp.headers.multi_items()
               if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "content-type"}]
    content_type = resp.headers.get("content-type", "application/json")
    if resp.status_code == 200:
        try:
            native = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise CopilotRejectedResponse(raw.decode("utf-8", "replace"), "Copilot Responses body is not JSON") from error
        chat = _copilot_responses_to_chat_completion(native, model)
        from ouroboros.copilot_native_custody import observe_native_output
        observe_native_output(native, chat, resp.request)
        raw = _copilot_chat_completion_to_sse(chat) if stream else json.dumps(chat).encode("utf-8")
        content_type = "text/event-stream; charset=utf-8" if stream else "application/json"
    result = hm.Response(resp.status_code, headers=[*headers, ("content-type", content_type)],
                         content=raw, request=resp.request, extensions=resp.extensions, history=resp.history)
    try:
        result.elapsed = resp.elapsed
    except RuntimeError:
        pass  # An injected test response need not have crossed a timed client.
    return result


def attach_copilot_client(client: Any, *, httpx_mod: Any = None, async_client: bool = False):
    """Attach once above httpx mount selection, retaining its native routing.

    This is a public-send wrapper, not a private mount override or a second
    provider send. The native response finishes auth/redirect handling before
    conversion. The last currently registered request hook observes application
    requests after auth/earlier hooks; it does not attest TCP delivery or changes
    a later-installed hook might make. Evidence stays separate from SDK inputs.
    """
    if not copilot_endpoint(getattr(client, "base_url", None)):
        return client
    http = client._client
    if getattr(http, "_ouroboros_copilot_codec", False):
        return client
    if httpx_mod is None:
        import importlib
        # Inspect the actual client's base, not an optional SDK Default export:
        # compatible SDKs can select httpx2 even when that export is absent.
        backend = next(base.__module__.split(".")[0] for base in type(http).__mro__
                       if base.__module__.split(".")[0] in {"httpx", "httpx2"})
        httpx_mod = importlib.import_module(backend)
    async_client = isinstance(http, httpx_mod.AsyncClient)
    from ouroboros.llm_observability import retain_http_observation

    def observe(request):
        if copilot_endpoint(str(request.url)) and request.url.path.endswith("/responses"):
            from ouroboros.llm_observability import note_http_projection
            note_http_projection(request)
            retain_http_observation("request_hook", request)

    send = http.send
    if async_client:
        async def hook(request):
            if copilot_endpoint(str(request.url)) and request.url.path.endswith("/responses"):
                from ouroboros.llm_observability import note_http_projection
                from ouroboros._usage_wait import postresponse_off_loop
                note_http_projection(request)
                await postresponse_off_loop(retain_http_observation, "request_hook", request,
                                            retain_on_cancel=lambda result, capture: result)

        async def send_async(request, *args, **kwargs):
            prepared = _prepare_request(request, httpx_mod)
            if prepared is None:
                return await send(request, *args, **kwargs)
            native_request, model, stream = prepared
            resp = await send(native_request, *args, **kwargs)
            try:
                raw = await resp.aread()
            except BaseException:
                try:
                    await resp.aclose()
                except BaseException:
                    pass  # A cleanup error cannot mask the primary read/cancel.
                raise
            await resp.aclose()
            from functools import partial
            from ouroboros._usage_wait import postresponse_off_loop
            await postresponse_off_loop(partial(retain_http_observation, "native_response", resp.request,
                                               response=resp, body=raw),
                                        retain_on_cancel=lambda result, capture: result)
            return _convert_response(resp, raw, model, stream, httpx_mod)
        http.send = send_async
        http.event_hooks["request"].append(hook)
    else:
        def send_sync(request, *args, **kwargs):
            prepared = _prepare_request(request, httpx_mod)
            if prepared is None:
                return send(request, *args, **kwargs)
            native_request, model, stream = prepared
            resp = send(native_request, *args, **kwargs)
            try:
                raw = resp.read()
            except BaseException:
                try:
                    resp.close()
                except BaseException:
                    pass
                raise
            resp.close()
            retain_http_observation("native_response", resp.request, response=resp, body=raw)
            return _convert_response(resp, raw, model, stream, httpx_mod)
        http.send = send_sync
        http.event_hooks["request"].append(observe)
    http._ouroboros_copilot_codec = True
    return client
