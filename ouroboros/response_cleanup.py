"""Preserve decoded HTTP results/primary faults when response cleanup fails.

A response hook precedes httpx implicit reads and changes only close/aclose.
Iteration, decoding, elapsed metadata, mounts, auth and retries stay with HTTPX.
Native Requests streaming uses the same policy at its explicit close boundary.
Throwing cleanup is unconfirmed release, never OS release or retry authority.
"""
from __future__ import annotations

from functools import partial
import logging
import uuid


def _recording_owner():
    try:
        from ouroboros.usage_accounting import (
            current_physical_attempt_drive_root, current_usage_scope, last_physical_attempt_capture,
        )
        capture, root = last_physical_attempt_capture(), current_physical_attempt_drive_root()
        scope = current_usage_scope()
        return capture, root, scope.task_id if scope else "llm"
    except Exception:
        return None, None, "llm"  # Missing attribution is not a failed response.


def _record_failure(error: Exception, owner) -> None:
    """Existing private attempt custody; a failing sink cannot hide the read result."""
    try:
        from ouroboros.observability import persist_call
        capture, root, task_id = owner
        if capture is not None and root is not None:
            facts = {"attempt_id": capture.attempt_id, "connection_release": "unconfirmed",
                     "error_type": type(error).__name__}
            persist_call(root, task_id=task_id,
                         call_id=f"physical_{capture.attempt_id}_cleanup_{uuid.uuid4().hex}",
                         call_type="physical_cleanup", payload=facts, manifest=facts, keep_raw=True)
    except Exception:
        pass  # An unavailable evidence sink must not replace a received result/primary fault.
    try:
        logging.getLogger(__name__).warning("Response cleanup failed (%s); connection release unconfirmed",
                                            type(error).__name__)
    except Exception:
        pass  # A throwing logging handler is another evidence sink, not the result owner.


def _close(close, owner) -> None:
    try:
        close()
    except Exception as error:
        _record_failure(error, owner)


async def _aclose(close, owner) -> None:
    try:
        await close()
    except Exception as error:
        from ouroboros._usage_wait import postresponse_off_loop
        try:
            await postresponse_off_loop(_record_failure, error, owner, retain_on_cancel=None)
        except Exception:
            pass  # Off-loop recording failure is not a response failure either.


def close_response(response) -> None:
    _close(response.close, _recording_owner())


async def close_response_async(response) -> None:
    await _aclose(response.aclose, _recording_owner())


def protect_httpx_client(http, *, asynchronous: bool = False) -> None:
    """Attach to the final SDK client, including its default/proxy construction."""
    hooks = getattr(http, "event_hooks", None)
    if not isinstance(hooks, dict) or getattr(http, "_ouroboros_cleanup_hook", False):
        return  # SDK test doubles/non-httpx clients have no response hook surface.

    def protect(response):
        if not getattr(response, "_ouroboros_cleanup_guard", False):
            response.close = partial(_close, response.close, _recording_owner())
            response._ouroboros_cleanup_guard = True

    async def protect_async(response):
        if not getattr(response, "_ouroboros_cleanup_guard", False):
            response.aclose = partial(_aclose, response.aclose, _recording_owner())
            response._ouroboros_cleanup_guard = True

    hooks.setdefault("response", []).insert(0, protect_async if asynchronous else protect)
    http._ouroboros_cleanup_hook = True
