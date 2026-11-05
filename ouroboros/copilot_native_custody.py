"""Same-route Copilot Responses output custody, separate from model-visible prose.

The HTTP codec observes accepted native items; a scoped, out-of-band attachment
joins them to that send's consumed SDK result. Canonical text/calls remain the
execution authority. Send copies replay only an unchanged projection on the
same endpoint/model/credential; public copies omit custody, checkpoints keep it.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
from typing import Any, Mapping

from ouroboros.request_wire_contract import canonical_sha256, normalize_endpoint

COPILOT_NATIVE_KEY = "_copilot_native_output"
COPILOT_WIRE_KEY = "_copilot_replay_output"
_TOKEN = object()
_OUTPUT = contextvars.ContextVar("copilot_native_output", default=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _projection(message: Mapping) -> dict:
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        calls.append({"id": call.get("id"), "type": call.get("type"),
                      "function": {"name": fn.get("name"), "arguments": fn.get("arguments")}})
    return {"content": message.get("content") or "", "tool_calls": calls}


def _route(target: Mapping) -> dict:
    return {"provider": str(target.get("provider") or ""),
            "endpoint_sha256": canonical_sha256(normalize_endpoint(target.get("base_url"))),
            "model": str(target.get("resolved_model") or ""), "api_surface": "responses",
            "credential_sha256": canonical_sha256(str(target.get("api_key") or ""))}


def _eligible(target: Mapping) -> bool:
    from ouroboros.copilot_responses import uses_copilot_responses
    return (target.get("provider") == "openai-compatible"
            and uses_copilot_responses(target.get("base_url"), target.get("resolved_model")))


@contextlib.contextmanager
def native_output_scope():
    token = _OUTPUT.set([])
    try:
        yield
    finally:
        _OUTPUT.reset(token)


def observe_native_output(native: Mapping, chat: Mapping, request: Any) -> None:
    """Called only after the codec accepted a complete, non-refusal response."""
    slot = _OUTPUT.get()
    if slot is None or native.get("status") != "completed":
        return
    message = chat["choices"][0]["message"]
    items = []
    for item in native.get("output") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "reasoning":
            # Native IDs are opaque (Copilot uses base64, not an rs_ prefix).
            if (isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]
                    and isinstance(item.get("id"), str) and item["id"]):
                items.append({key: copy.deepcopy(item[key]) for key in
                              ("type", "id", "summary", "encrypted_content") if key in item})
        elif kind == "message":
            parts = item.get("content") or []
            if not isinstance(parts, list) or any(not isinstance(part, dict) or part.get("type") not in {"text", "output_text"} for part in parts):
                return  # This codec has no canonical projection for that content.
            row = {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": str(part.get("text") or "")} for part in parts]}
            if item.get("phase") is not None:
                row["phase"] = item["phase"]
            items.append(row)
        elif kind == "function_call":
            items.append({key: item[key] for key in ("type", "call_id", "name", "arguments")})
        else:
            return  # Unknown native execution semantics must not be invented.
    if not any(row.get("type") == "reasoning" or "phase" in row for row in items):
        return
    base = str(request.url.copy_with(path=request.url.path[:-len("/responses")]))
    slot.append({"endpoint_sha256": canonical_sha256(normalize_endpoint(base)),
                 "model": json.loads(request.content).get("model"), "items": items,
                 "projection_sha256": canonical_sha256(_projection(message))})


def bind_native_output(response: Any) -> Any:
    slot = _OUTPUT.get()
    if slot:
        object.__setattr__(response, "_ouroboros_copilot_output", (_TOKEN, slot[-1]))
    return response


def retain_native_output(message: dict, target: Mapping, response: Any) -> dict:
    """Reject provider-supplied keys; only the scoped HTTP result can mint custody."""
    message.pop(COPILOT_NATIVE_KEY, None)
    message.pop(COPILOT_WIRE_KEY, None)
    fact = getattr(response, "_ouroboros_copilot_output", None)
    if not (_eligible(target) and isinstance(fact, tuple) and len(fact) == 2 and fact[0] is _TOKEN):
        return message
    observed = fact[1]
    route = _route(target)
    projection = canonical_sha256(_projection(message))
    if (observed["endpoint_sha256"] != route["endpoint_sha256"] or observed["model"] != route["model"]
            or projection != observed["projection_sha256"]):
        return message
    encoded = _json(observed["items"])
    message[COPILOT_NATIVE_KEY] = {"version": 1, "route": route, "content_json": encoded,
                                 "content_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                                 "projection_sha256": projection}
    return message


def replay_items(message: Mapping, target: Mapping) -> list | None:
    receipt = message.get(COPILOT_NATIVE_KEY)
    if not isinstance(receipt, Mapping) or receipt.get("version") != 1 or receipt.get("route") != _route(target):
        return None
    if receipt.get("projection_sha256") != canonical_sha256(_projection(message)):
        return None
    encoded = receipt.get("content_json")
    if not isinstance(encoded, str) or hashlib.sha256(encoded.encode()).hexdigest() != receipt.get("content_sha256"):
        return None
    try:
        items = json.loads(encoded)
    except ValueError:
        return None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return None
    # A resumed/tampered receipt is a cache miss, not a second authority for
    # text, executable calls, or order. The canonical fields still decide.
    native_text, native_calls = [], []
    for item in items:
        if item.get("type") == "message":
            parts = item.get("content")
            if not isinstance(parts, list) or any(not isinstance(part, dict) or not isinstance(part.get("text"), str) for part in parts):
                return None
            native_text.extend(part["text"] for part in parts)
        elif item.get("type") == "function_call":
            native_calls.append({"id": item.get("call_id"), "type": "function", "function": {
                "name": item.get("name"), "arguments": item.get("arguments")}})
        elif item.get("type") != "reasoning":
            return None
    if {"content": "".join(native_text), "tool_calls": native_calls} != _projection(message):
        return None
    # Rebuild visible text and calls from canonical, retaining native item order.
    text = _projection(message)["content"]
    calls = iter(_projection(message)["tool_calls"])
    offset = 0
    for item in items:
        if item["type"] == "message":
            for part in item["content"]:
                length = len(part["text"])
                part["text"] = text[offset:offset + length]
                offset += length
        elif item["type"] == "function_call":
            call = next(calls)
            item.update(call_id=call["id"], name=call["function"]["name"], arguments=call["function"]["arguments"])
    return items


def prepare_native_replay(original: list, prepared: list, target: Mapping) -> list:
    if not _eligible(target):
        return prepared
    for source, row in zip(original, prepared):
        if row.get("role") == "assistant":
            items = replay_items(source, target)
            if items is not None and _projection(source) == _projection(row):
                row[COPILOT_WIRE_KEY] = items
    return prepared


def has_native_replay(messages: list) -> bool:
    return any(isinstance(row, dict) and COPILOT_WIRE_KEY in row for row in messages)
