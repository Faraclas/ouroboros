"""Save the composer's effort and live Subagents choice for its room."""

from __future__ import annotations

import os
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.composer_cognition import main_cognition, request_project_id, update_project_cognition
from ouroboros.gateway._helpers import request_drive_root
from ouroboros.gateway.owner_settings import (
    _owner_audit,
    _owner_update_settings,
    owner_write_guard,
    settings_document_digest,
    settings_document_mutation,
    unsaved_error,
)
from ouroboros.settings_scales import EFFORT_FORMAT_KEY, EFFORT_RANGE_KEYS, EFFORT_SCALE, effort_range, effort_rank

# The body's fields, in the order of ``EFFORT_RANGE_KEYS`` (min, recommended = TASK, max).
RANGE_FIELDS = ("min", "recommended", "max")


def effort_range_refusal(body: Any) -> str:
    """A complete triple of tiers with min ≤ max; primary recommended is independent."""
    if not isinstance(body, dict):
        return "JSON body must be an object with min, recommended and max."
    tiers: Dict[str, str] = {}
    for field in RANGE_FIELDS:
        value = str(body.get(field) or "").strip().lower()
        if value not in EFFORT_SCALE:
            return f"'{field}' must be one of: {', '.join(EFFORT_SCALE)}."
        tiers[field] = value
    if effort_rank(tiers["min"]) > effort_rank(tiers["max"]):
        return "The helper range must be ordered min ≤ max."
    return ""


@owner_write_guard
async def api_owner_effort_range(request: Request) -> JSONResponse:
    """Persist the owner's effort range; every participant started afterwards reads it."""
    from ouroboros.gateway.settings import _json_body_or_empty, _run_settings_writer

    body = await _json_body_or_empty(request)
    # Off the event loop, under the document lock (held inside), like every settings writer.
    return await _run_settings_writer(_api_owner_effort_range_sync, request, body)


def _api_owner_effort_range_sync(request: Request, body: Any) -> JSONResponse:
    refusal = effort_range_refusal(body)
    if refusal:
        return unsaved_error(refusal, 400, code="effort_range_invalid")
    try:
        project_id = request_project_id(body)
    except ValueError as exc:
        return unsaved_error(str(exc), 400, code="effort_range_invalid")
    values = {key: str(body[field]).strip().lower() for key, field in zip(EFFORT_RANGE_KEYS, RANGE_FIELDS)}
    current = effort_range(values)

    def _set_range(current: Dict[str, Any]) -> Dict[str, Any]:
        current.update(values)
        current[EFFORT_FORMAT_KEY] = 1
        return current

    with settings_document_mutation():
        if project_id:
            try:
                old, cognition = update_project_cognition(request_drive_root(request), project_id, effort_range=current)
            except ValueError as exc:
                return unsaved_error(str(exc), 404, code="project_unavailable")
            previous = old["effort_range"]
        else:
            previous = main_cognition()["effort_range"]
            digest = settings_document_digest()
            _owner_update_settings(_set_range, digest, authored_keys=EFFORT_RANGE_KEYS)
            for key, value in values.items():
                os.environ[key] = value
            cognition = main_cognition()
    _owner_audit(request, "effort_range", {"project_id": project_id, "effort_range": current, "previous_effort_range": previous})
    return JSONResponse({"ok": True, "project_id": project_id, "effort_range": current, "cognition": cognition})


@owner_write_guard
async def api_owner_subagents_mode(request: Request) -> JSONResponse:
    from ouroboros.gateway.settings import _json_body_or_empty, _run_settings_writer

    return await _run_settings_writer(_api_owner_subagents_mode_sync, request, await _json_body_or_empty(request))


def _api_owner_subagents_mode_sync(request: Request, body: Any) -> JSONResponse:
    from ouroboros.config import VALID_SUBAGENTS_MODES

    mode = str(body.get("mode") or "").strip().lower() if isinstance(body, dict) else ""
    if mode not in VALID_SUBAGENTS_MODES:
        return unsaved_error("Mode must be off, auto or swarm.", 400, code="subagents_mode_invalid")
    try:
        project_id = request_project_id(body)
    except ValueError as exc:
        return unsaved_error(str(exc), 400, code="subagents_mode_invalid")
    with settings_document_mutation():
        if project_id:
            try:
                old, cognition = update_project_cognition(request_drive_root(request), project_id, subagents_mode=mode)
            except ValueError as exc:
                return unsaved_error(str(exc), 404, code="project_unavailable")
            previous = old["subagents_mode"]
        else:
            previous = main_cognition()["subagents_mode"]
            _owner_update_settings(lambda current: {**current, "OUROBOROS_SUBAGENTS_MODE": mode},
                                   settings_document_digest(), authored_keys=("OUROBOROS_SUBAGENTS_MODE",))
            os.environ["OUROBOROS_SUBAGENTS_MODE"] = mode
            cognition = main_cognition()
    _owner_audit(request, "subagents_mode", {"project_id": project_id, "subagents_mode": mode, "previous_subagents_mode": previous})
    return JSONResponse({"ok": True, "project_id": project_id, "subagents_mode": mode, "cognition": cognition})


@owner_write_guard
async def api_owner_primary_model(request: Request) -> JSONResponse:
    from ouroboros.gateway.settings import _json_body_or_empty, _run_settings_writer

    body = dict(request.query_params) if request.method == "GET" else await _json_body_or_empty(request)
    return await _run_settings_writer(_api_owner_primary_model_sync, request, body)


def _api_owner_primary_model_sync(request: Request, body: Any) -> JSONResponse:
    from ouroboros.composer_cognition import (
        PrimaryModelConflict, normalize_primary, read_cognition,
    )
    from ouroboros.config import load_settings
    from ouroboros.model_slots import MODEL_ACCOUNTS_KEY, apply_model_role_override

    try:
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        project_id = request_project_id(body)
        if request.method == "GET":
            cognition = read_cognition(request_drive_root(request), project_id)
        else:
            desired = normalize_primary(body.get("primary_model"))
            expected = normalize_primary(body.get("expected_primary_model"))
            with settings_document_mutation():
                if project_id:
                    _old, cognition = update_project_cognition(
                        request_drive_root(request), project_id, primary_model=desired, expected_primary_model=expected)
                else:
                    digest = settings_document_digest()
                    if main_cognition()["primary_model"] != expected:
                        raise PrimaryModelConflict("Primary model changed elsewhere. Check the current model before applying.")
                    _owner_update_settings(lambda current: apply_model_role_override(current, role="main", **desired),
                                           digest, authored_keys=("OUROBOROS_MODEL", "USE_LOCAL_MAIN", MODEL_ACCOUNTS_KEY),
                                           allow_safety_lowering=True)
                    from ouroboros.config import apply_settings_to_env
                    apply_settings_to_env(load_settings())
                    cognition = main_cognition()
            _owner_audit(request, "primary_model", {"project_id": project_id, "primary_model": desired})
    except PrimaryModelConflict as exc:
        return unsaved_error(str(exc), 409, code="primary_model_conflict")
    except ValueError as exc:
        return unsaved_error(str(exc), 400, code="primary_model_invalid")
    return JSONResponse({"ok": True, "project_id": project_id, "primary_model": cognition["primary_model"], "cognition": cognition})


__all__ = ["RANGE_FIELDS", "api_owner_effort_range", "api_owner_subagents_mode", "api_owner_primary_model", "effort_range_refusal"]
