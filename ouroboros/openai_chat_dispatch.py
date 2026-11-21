"""Direct-OpenAI Chat custom-tool dispatch and ephemeral validation custody.

The canonical request and transcript remain function-shaped.  This leaf owns
the physical custom projection, the owner-selected same-route candidate order,
response normalization, and the non-history validation sidecar consumed
before any custom-origin call reaches a handler.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from ouroboros.openai_chat_custom import (
    _SCHEMA_ARRAY_KEYWORDS,
    _SCHEMA_MAP_KEYWORDS,
    _SCHEMA_PROSE_KEYS,
    _SCHEMA_VALUE_KEYWORDS,
    CustomToolProjectionError,
    custom_receipts_prove_wire_acceptance,
    normalize_openai_custom_tool_calls,
    project_function_tool_request_to_openai_custom,
    project_messages_for_openai_custom,
)
from ouroboros.request_wire_contract import (
    EphemeralWireAdjustment,
    apply_effort_action,
    canonical_sha256,
    infer_tool_dialect,
    payload_effort,
)
from ouroboros.request_wire_custom_validation import CustomArgumentValidationReceipt
from ouroboros.request_wire_receipts import (
    WireAppliedAction,
    WireCandidateManifest,
    WireCandidateSpec,
    bind_wire_candidate,
    observe_wire_semantics,
)

REQUEST_WIRE_USAGE_KEY = "request_wire"
CUSTOM_RECEIPTS_USAGE_KEY = "_request_wire_custom_receipts"

_DIALECT_REJECTION_CODES = frozenset({
    "invalid_parameter",
    "invalid_request_error",
    "invalid_value",
    "unsupported_parameter",
    "unsupported_value",
})
_DIALECT_REJECTION_MARKERS = (
    "not supported",
    "unsupported",
    "not allowed",
    "not compatible",
    "cannot be used",
    "only supports",
    "must use",
)
_HISTORY_ERROR_MARKERS = (
    "followed by",
    "history",
    "matching tool result",
    "previous tool",
    "tool result for",
    "tool message for",
    "tool_call_id",
)


_LITERAL_VALUE_KEYS = _SCHEMA_PROSE_KEYS | frozenset({"enum"})
_TYPE_SPECIFIC_KEYWORDS: Dict[str, frozenset[str]] = {
    "array": frozenset({
        "additionalItems",
        "contains",
        "items",
        "maxContains",
        "maxItems",
        "minContains",
        "minItems",
        "prefixItems",
        "unevaluatedItems",
        "uniqueItems",
    }),
    "string": frozenset({
        "contentEncoding",
        "contentMediaType",
        "contentSchema",
        "format",
        "maxLength",
        "minLength",
        "pattern",
    }),
    "number": frozenset({
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maximum",
        "minimum",
        "multipleOf",
    }),
    "integer": frozenset({
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maximum",
        "minimum",
        "multipleOf",
    }),
    "object": frozenset({
        "additionalProperties",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "maxProperties",
        "minProperties",
        "patternProperties",
        "properties",
        "propertyNames",
        "required",
        "unevaluatedProperties",
    }),
}


def _matches_json_type(value: Any, json_type: str) -> bool:
    if json_type == "null":
        return value is None
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, Mapping)
    return True


def _normalize_schema_multitypes(node: Any) -> Any:
    """Recursively normalize >=2 non-null ``type`` lists into portable ``anyOf``."""
    if isinstance(node, bool):
        return node
    if not isinstance(node, Mapping):
        return copy.deepcopy(node)

    normalized: Dict[str, Any] = {}
    for key, value in node.items():
        key_str = str(key)
        if key_str in _LITERAL_VALUE_KEYS:
            normalized[key_str] = copy.deepcopy(value)
        elif key_str in _SCHEMA_VALUE_KEYWORDS:
            normalized[key_str] = _normalize_schema_multitypes(value)
        elif key_str in _SCHEMA_ARRAY_KEYWORDS and isinstance(value, list):
            normalized[key_str] = [_normalize_schema_multitypes(item) for item in value]
        elif key_str in _SCHEMA_MAP_KEYWORDS and isinstance(value, Mapping):
            normalized[key_str] = {
                str(k): _normalize_schema_multitypes(v) for k, v in value.items()
            }
        elif key_str == "items":
            if isinstance(value, list):
                normalized[key_str] = [_normalize_schema_multitypes(item) for item in value]
            else:
                normalized[key_str] = _normalize_schema_multitypes(value)
        elif key_str == "dependencies" and isinstance(value, Mapping):
            normalized[key_str] = {
                str(k): (
                    _normalize_schema_multitypes(v)
                    if isinstance(v, (Mapping, bool))
                    else copy.deepcopy(v)
                )
                for k, v in value.items()
            }
        else:
            normalized[key_str] = copy.deepcopy(value)

    raw_type = normalized.get("type")
    if (
        isinstance(raw_type, Sequence)
        and not isinstance(raw_type, (str, bytes, bytearray))
    ):
        seen_types: List[str] = []
        for item in raw_type:
            if isinstance(item, str) and item not in seen_types:
                seen_types.append(item)
        non_null = [t for t in seen_types if t != "null"]
        has_null = "null" in seen_types
        if len(non_null) >= 2:
            branch_types = list(non_null) + (["null"] if has_null else [])
            raw_enum = normalized.get("enum")
            has_enum = isinstance(raw_enum, list)
            moved_keywords: set[str] = set()
            branches: List[Dict[str, Any]] = []
            for branch_type in branch_types:
                branch: Dict[str, Any] = {"type": branch_type}
                for kw in _TYPE_SPECIFIC_KEYWORDS.get(branch_type, ()):
                    if kw in normalized:
                        branch[kw] = copy.deepcopy(normalized[kw])
                        moved_keywords.add(kw)
                if has_enum:
                    matching_enum = [
                        copy.deepcopy(v)
                        for v in raw_enum
                        if _matches_json_type(v, branch_type)
                    ]
                    if matching_enum:
                        branch["enum"] = matching_enum
                    else:
                        continue
                branches.append(branch)

            if has_enum and branches:
                normalized.pop("enum", None)
            elif has_enum and not branches:
                branches = [{"type": t} for t in branch_types]
                for branch in branches:
                    for kw in _TYPE_SPECIFIC_KEYWORDS.get(branch["type"], ()):
                        if kw in normalized:
                            branch[kw] = copy.deepcopy(normalized[kw])
                            moved_keywords.add(kw)

            normalized.pop("type", None)
            for kw in moved_keywords:
                normalized.pop(kw, None)

            if "anyOf" not in normalized:
                normalized["anyOf"] = branches
            else:
                existing_anyof = normalized.pop("anyOf")
                all_of = (
                    list(normalized["allOf"])
                    if isinstance(normalized.get("allOf"), list)
                    else []
                )
                all_of.extend([{"anyOf": existing_anyof}, {"anyOf": branches}])
                normalized["allOf"] = all_of

    return normalized


def sanitize_function_tools(
    tools: Optional[Sequence[Mapping[str, Any]]],
    *,
    description_normalizer: Callable[[Any], str],
    on_drop: Optional[Callable[[str, str], None]] = None,
) -> list[Dict[str, Any]]:
    """Build the deterministic effective function catalog used by every projection."""
    prepared = []
    seen = set()
    name_pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    for tool in tools or ():
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        reason = ""
        if not name or not name_pattern.fullmatch(name):
            reason = "invalid"
        elif name in seen:
            reason = "duplicate"
        if reason:
            if on_drop is not None:
                on_drop(reason, name)
            continue
        seen.add(name)
        function_copy = dict(function)
        function_copy["name"] = name
        function_copy["description"] = description_normalizer(
            function_copy.get("description")
        )
        params = function_copy.get("parameters")
        if not isinstance(params, Mapping):
            function_copy["parameters"] = {"type": "object", "properties": {}}
        else:
            function_copy["parameters"] = _normalize_schema_multitypes(params)
        tool_copy = dict(tool)
        tool_copy["function"] = function_copy
        prepared.append(tool_copy)
    prepared.sort(key=lambda item: str((item.get("function") or {}).get("name") or ""))
    return prepared


def _error_facts(value: Any) -> Tuple[int, str, str, str]:
    """Return status/code/param/message from an exception or body-error response."""
    status = getattr(value, "status_code", None)
    body = getattr(value, "body", None)
    if not isinstance(body, Mapping):
        try:
            dumped = value.model_dump()
        except Exception:
            dumped = None
        body = dumped if isinstance(dumped, Mapping) else {}
    error = body.get("error") if isinstance(body.get("error"), Mapping) else body
    try:
        status_code = int(
            status
            if status is not None
            else error.get("status_code") or error.get("status") or error.get("code") or 0
        )
    except (TypeError, ValueError):
        status_code = 0
    code = str(
        getattr(value, "code", "")
        or error.get("code")
        or error.get("type")
        or ""
    ).strip().lower()
    param = str(
        getattr(value, "param", "")
        or error.get("param")
        or ""
    ).strip().lower()
    message = str(
        getattr(value, "message", "")
        or error.get("message")
        or value
        or ""
    ).strip().lower()
    return status_code, code, param, message


def exact_tool_dialect_rejection(
    value: Any,
    candidate: WireCandidateManifest,
) -> bool:
    """Narrow 4xx classifier for the exact function/custom physical dialect."""
    status, code, param, message = _error_facts(value)
    if status not in {400, 422}:
        return False
    dialect = candidate.accepted_profile.tool_dialect
    dialect_word = "custom" if dialect == "openai_chat_custom" else "function"
    tool_param = bool(
        param in {"tools", "tool_choice"}
        or param.startswith(("tools[", "tools.", "tool_choice[", "tool_choice."))
    )
    rejection_named = any(
        marker in message for marker in _DIALECT_REJECTION_MARKERS
    )
    if tool_param:
        return bool(code in _DIALECT_REJECTION_CODES or rejection_named)
    transcript_param = bool(
        param == "messages"
        or param.startswith(("messages[", "messages."))
        or "tool_call_id" in param
    )
    if transcript_param or any(marker in message for marker in _HISTORY_ERROR_MARKERS):
        return False
    dialect_named = bool(
        re.search(rf"\b{dialect_word}\s+(?:tools?|calls?|calling)\b", message)
        or re.search(
            rf"\btools?\s+(?:of\s+)?type\s+['\"]?{dialect_word}\b",
            message,
        )
    )
    return bool(dialect_named and rejection_named)


def is_direct_openai_ladder_candidate(candidate: WireCandidateManifest) -> bool:
    """Whether a manifest belongs to the owner-selected custom/function/none rail."""
    return bool(
        candidate.source_profile.provider == "openai"
        and candidate.source_profile.api_surface == "chat.completions"
        and candidate.source_profile.tool_dialect == "function"
        and candidate.requested_effort != "none"
        and candidate.ladder_ordinal in {1, 2, 3}
    )


def _applied_effort(
    requested: str,
    actions: Sequence[WireAppliedAction],
) -> str:
    effort = requested
    for item in actions:
        action = item.action
        if action.get("kind") == "set_value":
            effort = apply_effort_action(effort, action)
        elif action.get("kind") == "drop_field" and set(
            action.get("fields") or ()
        ) & {"reasoning_effort", "thinking", "output_config", "extra_body.reasoning"}:
            effort = "provider_default"
    return effort


def plan_direct_openai_dialect_candidate(
    *,
    target: Mapping[str, Any],
    source_payload: Mapping[str, Any],
    current: WireCandidateManifest,
    error: Any,
    body_error: bool,
) -> Optional[WireCandidateManifest]:
    """Advance one cognition-preserving rung, or create the final task-local none."""
    if not is_direct_openai_ladder_candidate(current):
        return None
    value = _BodyError({"error": dict(error)}) if body_error and isinstance(
        error, Mapping
    ) else error
    if not isinstance(value, BaseException) or not exact_tool_dialect_rejection(
        value, current,
    ):
        return None
    requested = current.requested_effort
    durable = tuple(
        item for item in current.applied_actions
        if item.source == "durable"
        and item.profile.tool_dialect == "function"
        and item.action.get("kind") != "replace_dialect"
    )
    api_surface = current.source_profile.api_surface
    if current.accepted_profile.tool_dialect == "openai_chat_custom":
        return bind_wire_candidate(
            target=target,
            api_surface=api_surface,
            source_payload=source_payload,
            candidate_spec=WireCandidateSpec(
                "function",
                _applied_effort(requested, durable),
                "provider_rejected_tool_dialect",
            ),
            requested_effort=requested,
            ladder_ordinal=2,
            applied_actions=durable,
        )
    if current.accepted_profile.tool_dialect != "function":
        return None
    baseline = bind_wire_candidate(
        target=target,
        api_surface=api_surface,
        source_payload=source_payload,
        candidate_spec=WireCandidateSpec(
            "function",
            _applied_effort(requested, durable),
            "provider_rejected_tool_dialect",
        ),
        requested_effort=requested,
        ladder_ordinal=2,
        applied_actions=durable,
    )
    adjustment = EphemeralWireAdjustment(baseline.accepted_profile, {
        "kind": "set_value",
        "field": "effort",
        "mode": "exact",
        "from": payload_effort(baseline.physical_payload()),
        "to": "none",
        "reason_code": "task_local_availability_fallback",
    })
    return bind_wire_candidate(
        target=target,
        api_surface=api_surface,
        source_payload=source_payload,
        candidate_spec=WireCandidateSpec(
            "function", "none", "task_local_availability_fallback", task_local=True,
        ),
        requested_effort=requested,
        ladder_ordinal=3,
        applied_actions=(*durable, WireAppliedAction.task_local(adjustment)),
    )


class _BodyError(Exception):
    def __init__(self, body: Mapping[str, Any]):
        self.body = body
        error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
        self.status_code = error.get("status_code") or error.get("status") or error.get("code")
        self.code = error.get("code")
        self.param = error.get("param")
        super().__init__(str(error.get("message") or "provider body error"))

def normalize_direct_openai_completion(
    message: Mapping[str, Any],
    usage: MutableMapping[str, Any],
    bound: Any,
) -> Tuple[Dict[str, Any], MutableMapping[str, Any]]:
    """Normalize custom calls; the shared recovery finalizer owns commit/disclosure."""
    canonical_message = dict(message)
    del bound
    from ouroboros.request_wire_recovery import current_wire_candidate

    candidate = current_wire_candidate()
    if candidate is None:
        return canonical_message, usage
    receipts: Tuple[CustomArgumentValidationReceipt, ...] = ()
    if candidate.accepted_profile.tool_dialect == "openai_chat_custom":
        calls = canonical_message.get("tool_calls") or []
        if calls:
            normalized, receipts = normalize_openai_custom_tool_calls(calls, candidate)
            canonical_message["tool_calls"] = normalized
        content = canonical_message.get("content")
        text_success = (
            not calls
            and isinstance(content, str)
            and bool(content.strip())
            and "provider_error" not in usage
        )
        if text_success or custom_receipts_prove_wire_acceptance(receipts):
            observe_wire_semantics(
                candidate=candidate,
                normalized_response=canonical_message,
                normalized_usage=usage,
                custom_receipts=receipts,
            )
    if receipts:
        usage[CUSTOM_RECEIPTS_USAGE_KEY] = receipts
    return canonical_message, usage


def pop_custom_validation_receipts(
    usage: MutableMapping[str, Any],
    tool_calls: Sequence[Mapping[str, Any]],
) -> Tuple[CustomArgumentValidationReceipt, ...]:
    """Pop and exact-call-bind the ephemeral custom validation sidecar."""
    raw = usage.pop(CUSTOM_RECEIPTS_USAGE_KEY, ())
    if not raw:
        return ()
    if not isinstance(raw, tuple) or any(
        not isinstance(item, CustomArgumentValidationReceipt) for item in raw
    ):
        raise ValueError("custom validation sidecar is not parser-issued")
    canonical_calls = {}
    for call in tool_calls:
        if not isinstance(call, Mapping) or call.get("type") != "function":
            raise ValueError("custom validation requires canonical function calls")
        call_id = str(call.get("id") or "")
        function = call.get("function")
        if not call_id or not isinstance(function, Mapping):
            raise ValueError("custom validation requires canonical function calls")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, str):
            raise ValueError("custom validation requires exact canonical calls")
        if call_id in canonical_calls:
            raise ValueError("custom validation sidecar differs from canonical calls")
        canonical_calls[call_id] = (name, arguments)
    call_ids = tuple(canonical_calls)
    receipt_ids = tuple(item.tool_call_id for item in raw)
    if (
        not call_ids
        or len(call_ids) != len(set(call_ids))
        or set(call_ids) != set(receipt_ids)
        or len(receipt_ids) != len(set(receipt_ids))
    ):
        raise ValueError("custom validation sidecar differs from canonical calls")
    candidates = {item.candidate_sha256 for item in raw}
    catalogs = {item.catalog_sha256 for item in raw}
    if len(candidates) != 1 or len(catalogs) != 1:
        raise ValueError("custom validation sidecar spans physical candidates")
    disclosure = usage.get(REQUEST_WIRE_USAGE_KEY)
    if (
        not isinstance(disclosure, Mapping)
        or disclosure.get("candidate_sha256") not in candidates
    ):
        raise ValueError("custom validation sidecar differs from physical candidate")
    for receipt in raw:
        name, arguments = canonical_calls[receipt.tool_call_id]
        if (
            receipt.tool_name != name
            or receipt.arguments_sha256 != canonical_sha256(arguments)
        ):
            raise ValueError("custom validation sidecar differs from canonical calls")
    return raw


def custom_validation_by_call_id(
    receipts: Sequence[CustomArgumentValidationReceipt],
) -> Dict[str, CustomArgumentValidationReceipt]:
    if any(not isinstance(item, CustomArgumentValidationReceipt) for item in receipts):
        raise ValueError("custom validation requires parser-issued receipts")
    result = {item.tool_call_id: item for item in receipts}
    if len(result) != len(receipts):
        raise ValueError("custom validation contains duplicate call ids")
    return result


def custom_tool_argument_error(
    tool_name: str,
    receipt: CustomArgumentValidationReceipt,
) -> str:
    return (
        "⚠️ TOOL_ARG_ERROR: Arguments for "
        f"'{str(tool_name or receipt.tool_name)}' failed the exact custom-tool schema "
        f"({receipt.error_code}). Correct the arguments and call the tool again."
    )


def custom_tool_error_continuation(
    message: Mapping[str, Any],
    receipts: Sequence[CustomArgumentValidationReceipt],
) -> list[Dict[str, Any]]:
    """Return canonical assistant + role=tool errors for invalid custom calls only."""
    by_id = custom_validation_by_call_id(receipts)
    continuation = [dict(message)]
    for call in message.get("tool_calls") or []:
        receipt = by_id.get(str(call.get("id") or ""))
        if receipt is None:
            raise ValueError("custom continuation lacks an exact call receipt")
        function = call.get("function") if isinstance(call, Mapping) else {}
        name = str(function.get("name") or "") if isinstance(function, Mapping) else ""
        content = (
            "⚠️ TOOL_ARG_ERROR: This custom-tool batch was not accepted because "
            "another call failed exact schema validation. Call the tool again."
            if receipt.allows_execution
            else custom_tool_argument_error(name, receipt)
        )
        continuation.append({
            "role": "tool",
            "tool_call_id": receipt.tool_call_id,
            "content": content,
        })
    return continuation


def direct_openai_context_projections(
    messages: Sequence[Mapping[str, Any]],
    tools: Optional[Sequence[Mapping[str, Any]]],
    *,
    provider: str,
    reasoning_effort: str,
    tool_choice: Any = "auto",
) -> Tuple[Tuple[list[Dict[str, Any]], list[Dict[str, Any]]], ...]:
    """Return every distinct transcript/catalog projection the ladder may send."""
    canonical_messages = [copy.deepcopy(dict(item)) for item in messages]
    canonical_tools = [copy.deepcopy(dict(item)) for item in tools or []]
    if (
        str(provider or "").strip().lower() != "openai"
        or str(reasoning_effort or "").strip().lower() in {"", "none"}
        or infer_tool_dialect({"tools": canonical_tools}) != "function"
    ):
        return ((canonical_messages, canonical_tools),)
    try:
        projected = project_function_tool_request_to_openai_custom(
            canonical_tools,
            tool_choice,
        )
        custom_messages = project_messages_for_openai_custom(
            canonical_messages,
            projected.catalog,
        )
        custom_tools = projected.catalog.wire_tools()
    except CustomToolProjectionError:
        return ((canonical_messages, canonical_tools),)
    return (
        (canonical_messages, canonical_tools),
        (custom_messages, custom_tools),
    )


def projected_context_size_bytes(
    messages: Sequence[Mapping[str, Any]],
    tools: Optional[Sequence[Mapping[str, Any]]],
    *,
    provider: str,
    reasoning_effort: str,
    tool_choice: Any = "auto",
) -> int:
    projections = direct_openai_context_projections(
        messages, tools, provider=provider, reasoning_effort=reasoning_effort,
        tool_choice=tool_choice,
    )
    return max(
        len(json.dumps(
            {"messages": projected_messages, "tools": projected_tools},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"))
        for projected_messages, projected_tools in projections
    )


__all__ = (
    "CUSTOM_RECEIPTS_USAGE_KEY",
    "REQUEST_WIRE_USAGE_KEY",
    "custom_tool_argument_error",
    "custom_tool_error_continuation",
    "custom_validation_by_call_id",
    "direct_openai_context_projections",
    "exact_tool_dialect_rejection",
    "is_direct_openai_ladder_candidate",
    "normalize_direct_openai_completion",
    "plan_direct_openai_dialect_candidate",
    "pop_custom_validation_receipts",
    "projected_context_size_bytes",
    "sanitize_function_tools",
)
