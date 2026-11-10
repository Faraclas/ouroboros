"""Owner-source-bound NEW Project authorization for an exact saved continuation.

This owner endpoint records scope only. It never schedules a second writer or
releases Pause, Stop, money or custody; the ordinary Resume door remains separate.
"""
from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_error, json_exception, request_json_or, run_sync_to_completion
from ouroboros.task_results import validate_task_id

_FIELDS = {'request_id', 'issuer_task_id', 'owner_source', 'checkpoint_sha256', 'project_basis'}


async def api_project_authorization(request: Request) -> JSONResponse:
    try:
        task_id = validate_task_id(request.path_params.get('task_id'))
    except ValueError as exc:
        return json_error(str(exc), 400)
    body = await request_json_or(request, {})
    if (not isinstance(body, dict) or set(body) != _FIELDS
            or any(not isinstance(body[key], str) or not body[key] for key in (
                'request_id', 'issuer_task_id', 'checkpoint_sha256'))
            or not isinstance(body['owner_source'], dict) or not isinstance(body['project_basis'], dict)):
        return json_error('project authorization requires an exact saved source and owner decision', 400,
                          reason_code='invalid_project_authorization')
    try:
        from supervisor.project_reauthorization import authorize_project_recovery

        result = await run_sync_to_completion(authorize_project_recovery, task_id, **body)
    except Exception as exc:
        return json_exception(exc, 503)
    if result.get('ok'):
        return JSONResponse(result)
    return json_error(result.get('error', 'project_authorization_refused'), 409, task_id=task_id,
                      reason_code=result.get('error', 'project_authorization_refused'),
                      **({'blockers': result['blockers']} if result.get('blockers') else {}))
