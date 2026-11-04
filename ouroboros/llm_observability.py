"""Observed LLM call helpers for non-loop decision surfaces."""

from __future__ import annotations

import pathlib
import logging
import contextlib
import contextvars
from typing import Any, Dict, Tuple

from ouroboros.observability import new_call_id, persist_call
from ouroboros.anthropic_native_custody import public_custody_projection
from ouroboros.utils import sanitize_tool_result_for_log


def retain_cancelled_response(request, response, capture, *, control_reason: str = "caller_cancelled") -> dict:
    """Keep a received physical response before cancellation unwinds its caller.

    Reuse private CAS and the ordinary call reader. This records the provider
    object (model_dump for SDK responses), not a completed task/review verdict.
    Native transports continue to own any original wire-byte receipt separately.
    """
    return _retain_physical_response(request, response, capture, control_reason=control_reason)


def _retain_physical_response(request, response, capture, *, control_reason: str) -> dict:
    from dataclasses import asdict

    if callable(getattr(response, "model_dump", None)):
        body = response.model_dump()
    elif isinstance(response, (dict, list)):
        body = response
    elif callable(getattr(response, "json", None)):
        body = response.json()
    else:
        # Never consume a stream or pass an SDK repr off as its complete body.
        raise TypeError(f"Unsupported physical response body: {type(response).__name__}")
    return persist_call(
        request.drive_root, task_id=request.task_id or "llm",
        call_id=f"physical_{capture.attempt_id}_response", call_type="physical_response",
        payload={"response": body, "physical_attempt_capture": asdict(capture)}, keep_raw=True,
        manifest={"attempt_id": capture.attempt_id, "model": capture.model,
                  "provider": capture.provider, "state": capture.state,
                  "status": "received", "control_reason": control_reason},
    )


def retain_received_response(request, response, *, capture=None, control_reason="received") -> dict:
    """Keep the complete materialized body before lane normalization, best-effort.

    Use only this execution's capture/root, not a prior attempt or a guessed data
    directory. No response-only evidence is attached to the outbound transcript.
    Accounting state is preserved verbatim: receipt is not a settlement claim.
    """
    from dataclasses import replace
    from ouroboros.usage_accounting import (
        current_physical_attempt_drive_root, current_usage_scope, last_physical_attempt_capture,
    )

    try:
        current = last_physical_attempt_capture()
        capture = capture if capture is not None else current
        root = current_physical_attempt_drive_root()
        if (capture is None or current is None or root is None
                or capture.attempt_id != current.attempt_id
                or (capture.model, capture.provider) != (request.model, request.provider)):
            raise ValueError("matching physical response capture/root unavailable")
        scope = current_usage_scope()
        target = replace(request, drive_root=root,
                         task_id=request.task_id or (scope.task_id if scope else ""))
        return _retain_physical_response(target, response, capture, control_reason=control_reason)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Physical response retention gap (%s); useful response preserved", type(exc).__name__,
            exc_info=True,
        )
        return {}


def persist_observed_call(root: Any, *, payload: Any, writer: Any = None, **identity: Any) -> dict:
    """Best-effort public trace: opaque continuation never enters this projection.

    Failure to write observability cannot discard an already useful response or
    authorize another provider call. Private exact-byte transport custody uses
    persist_call directly and retains its own stronger acknowledgement contract.
    """
    try:
        return (writer or persist_call)(root, payload=public_custody_projection(payload), **identity)
    except Exception:
        logging.getLogger(__name__).warning("Failed to persist LLM observability payload", exc_info=True)
        return {}


def _root(drive_root: Any) -> pathlib.Path:
    try:
        return pathlib.Path(drive_root)
    except TypeError:
        return pathlib.Path("../data")


def _base_manifest(call_type: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "call_type": call_type,
        "model": kwargs.get("model"),
        "reasoning_effort": kwargs.get("reasoning_effort"),
        "max_tokens": kwargs.get("max_tokens"),
        "use_local": kwargs.get("use_local"),
    }


def chat_observed(
    llm: Any,
    *,
    drive_root: Any,
    task_id: str = "",
    call_type: str = "llm_call",
    **kwargs: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run ``llm.chat`` while preserving request/response/error payloads."""

    root = _root(drive_root)
    call_id = new_call_id(call_type)
    traced_kwargs = dict(kwargs)
    persist_observed_call(
        root,
        task_id=task_id or call_type,
        call_id=f"{call_id}_request",
        call_type=f"{call_type}_request",
        payload={"kwargs": traced_kwargs},
        manifest=_base_manifest(call_type, kwargs),
    )
    try:
        msg, usage = llm.chat(**kwargs)
    except Exception as exc:
        safe = sanitize_tool_result_for_log(f"{type(exc).__name__}: {exc}")
        persist_observed_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_error",
            call_type=f"{call_type}_error",
            payload={"error": f"{type(exc).__name__}: {exc}", "kwargs": traced_kwargs},
            manifest={**_base_manifest(call_type, kwargs), "status": "error", "error": safe},
        )
        raise
    try:
        from ouroboros.openai_chat_dispatch import CUSTOM_RECEIPTS_USAGE_KEY

        public_usage = dict(usage)
        public_usage.pop(CUSTOM_RECEIPTS_USAGE_KEY, None)
        persist_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_response",
            call_type=f"{call_type}_response",
            payload={
                "message": public_custody_projection(msg),
                "usage": public_usage,
            },
            manifest={**_base_manifest(call_type, kwargs), "status": "ok"},
        )
    except Exception:
        pass
    return msg, usage


_HTTP_OBSERVATIONS = contextvars.ContextVar("http_observations", default=None)
_HTTP_OBSERVATION_TOKEN = object()


@contextlib.contextmanager
def http_observation_scope():
    """One sender's host-only observations; no provider JSON is trusted as facts."""
    token = _HTTP_OBSERVATIONS.set([])
    try:
        yield
    finally:
        _HTTP_OBSERVATIONS.reset(token)


def note_http_projection(request: Any) -> None:
    observations = _HTTP_OBSERVATIONS.get()
    if observations is not None:
        try:
            observations.append(http_projection(request))
        except Exception:
            logging.getLogger(__name__).warning("HTTP projection observation gap", exc_info=True)


def bind_http_observations(response: Any) -> Any:
    observations = _HTTP_OBSERVATIONS.get()
    if observations:
        try:
            object.__setattr__(response, "_ouroboros_http_projection",
                               (_HTTP_OBSERVATION_TOKEN, observations[-1]))
        except Exception:
            logging.getLogger(__name__).warning("HTTP projection attachment gap", exc_info=True)
    return response


def response_http_projection(response: Any):
    fact = getattr(response, "_ouroboros_http_projection", None)
    return fact[1] if isinstance(fact, tuple) and len(fact) == 2 and fact[0] is _HTTP_OBSERVATION_TOKEN else None


def http_projection(request: Any) -> dict:
    """Secret-free outgoing application-body facts, joined to this send only."""
    from ouroboros.usage_accounting import last_physical_attempt_capture
    from ouroboros.request_wire_contract import physical_candidate_sha256
    import json

    capture = last_physical_attempt_capture()
    payload = json.loads(request.content)
    reasoning = payload.get("reasoning")
    return {
        "boundary": "httpx_request_hook", "api_surface": "responses",
        "attempt_id": capture.attempt_id if capture is not None else None,
        "sdk_candidate_sha256": capture.candidate_raw_sha256 if capture is not None else None,
        "payload_sha256": physical_candidate_sha256(payload),
        "model": payload.get("model"),
        "effort": reasoning.get("effort") if isinstance(reasoning, dict) else None,
        "delivery": "not_attested",
    }


def retain_http_observation(kind: str, request: Any, *, response: Any = None, body: bytes | None = None) -> dict:
    """Keep a header-free HTTP boundary observation in existing private CAS.

    This is distinct from the sealed pre-SDK candidate. A hook observes outgoing
    application bytes, not TCP delivery. Native response bytes are decoded; no
    representation is invented on a read fault. Missing capture or retention is
    a logged gap, never a reason to discard output or send again.
    """
    from ouroboros.usage_accounting import (
        current_physical_attempt_drive_root, current_usage_scope, last_physical_attempt_capture,
    )
    import hashlib
    import json

    try:
        capture = last_physical_attempt_capture()
        root = current_physical_attempt_drive_root()
        if capture is None or root is None or capture.state != "dispatched":
            raise ValueError("matching dispatched HTTP observation owner unavailable")
        scope = current_usage_scope()
        projection = http_projection(request)
        facts = {"kind": kind, "method": request.method,
                 "scheme": request.url.scheme, "host": request.url.host, "path": request.url.path,
                 "projection": projection}
        if kind == "request_hook":
            facts["request_body"] = json.loads(request.content)
        else:
            facts.update(status_code=response.status_code, decoded_body=body.decode("utf-8", "replace"),
                         decoded_body_sha256=hashlib.sha256(body).hexdigest())
        identity = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
        return persist_call(root, task_id=scope.task_id if scope else "llm",
                            call_id=f"physical_{capture.attempt_id}_http_{kind}_{identity[:16]}",
                            call_type="physical_http", payload=facts, keep_raw=True,
                            manifest={"attempt_id": capture.attempt_id, "boundary": kind,
                                      "sdk_candidate_sha256": capture.candidate_raw_sha256,
                                      "observation_sha256": identity})
    except Exception:
        logging.getLogger(__name__).warning("HTTP observation retention gap", exc_info=True)
        return {}
