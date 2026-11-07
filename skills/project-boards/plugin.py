"""Project Work Items PluginAPI 2.0 adapter; no controller or task-data reader."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.responses import JSONResponse

if __package__:
    from .board_store import BoardError, BoardStore, exact_keys
else:  # Direct source checks; the host loads the entry as a package.
    from board_store import BoardError, BoardStore, exact_keys

INTERFACE_ACTOR = {"surface": "interface", "label": "via Interface/API"}
STRING = {"type": "string"}
REFERENCE = {"type": "object", "additionalProperties": False,
             "properties": {"kind": {"enum": ["task", "row", "url"]}, "value": STRING, "label": STRING},
             "required": ["kind", "value"]}
PREREQUISITE = {"type": "object", "additionalProperties": False,
                "properties": {"item_id": STRING, "needs": STRING}, "required": ["item_id"]}
CHANGES = {"type": "object", "additionalProperties": False, "properties": {
    "title": STRING, "description": STRING, "outcome": STRING,
    "disposition": {"enum": ["idea", "planned", "done", "set_aside"]}, "archived": {"type": "boolean"},
    "references": {"type": "array", "items": REFERENCE},
    "prerequisites": {"type": "array", "items": PREREQUISITE}}}


def schema(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


def project_reader(data_dir):
    """Narrow installed-host dependency; intentionally no registry reimplementation."""
    try:
        from ouroboros.projects_registry import get_reserved_project
        if not callable(get_reserved_project):
            raise ImportError("get_reserved_project is not callable")
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Project Work Items requires the host's strict project registry reader.") from exc
    def read(project_id):
        return get_reserved_project(data_dir, project_id, strict=True)
    return read


def agent_actor(ctx, profile_reader=None):
    # The host injects ctx via its frozen calling convention, never JSON arguments.
    # Reuse the host's actual profile rule, including delegated-role fallback.
    # Cross-project targets and Presence remain governed by their existing grants.
    if profile_reader is None:
        from ouroboros.tool_access import active_tool_profile
        profile_reader = active_tool_profile
    if profile_reader(ctx) == "local_readonly_subagent":
        raise BoardError("readonly", "This actor's explicit read-only assignment does not allow board mutations.", 403)
    task_id = getattr(ctx, "task_id", None)
    if not isinstance(task_id, str) or not task_id.strip():
        raise BoardError("unavailable", "Host task attribution is unavailable.", 503)
    actor = {"surface": "agent", "label": "Ouroboros", "task_id": task_id}
    project_id = getattr(ctx, "project_id", "")
    if isinstance(project_id, str) and project_id:
        actor["source_project_id"] = project_id
    chat_id = getattr(ctx, "current_chat_id", None)
    if type(chat_id) in (str, int):
        actor["chat_id"] = chat_id
    return actor


def guarded(call):
    try:
        return call(), 200
    except BoardError as exc:
        return exc.payload(), exc.status


def register(api):
    # API is constructed by the host at the installation's canonical skill-state
    # root. Root/helper ToolContexts must not move this DB to their private drive.
    try:
        from ouroboros.tool_access import active_tool_profile
        if not callable(active_tool_profile):
            raise ImportError("active_tool_profile is not callable")
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Project Work Items requires the host's canonical tool-profile reader.") from exc
    state_dir = Path(api.get_state_dir())
    data_dir = api.get_runtime_info().get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        raise RuntimeError("Canonical host data_dir is unavailable.")
    store = BoardStore(state_dir, project_reader(data_dir))

    def tool_result(call):
        payload, _status = guarded(call)
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)

    def board_find(ctx, project_id=None, q="", disposition=None, archived=False, offset=0, limit=100):
        return tool_result(lambda: store.find(
            getattr(ctx, "project_id", "") if project_id is None else project_id,
            q=q, disposition=disposition, archived=archived, offset=offset, limit=limit))

    def board_get(ctx, item_id):
        return tool_result(lambda: store.get(item_id))

    def board_capture(ctx, project_id, title, request_id, description=""):
        return tool_result(lambda: store.mutate("capture", {"project_id": project_id, "title": title,
            "description": description, "request_id": request_id}, agent_actor(ctx, active_tool_profile)))

    def board_update(ctx, item_id, expected_revision, request_id, changes):
        return tool_result(lambda: store.mutate("update", {"item_id": item_id, "expected_revision": expected_revision,
            "request_id": request_id, "changes": changes}, agent_actor(ctx, active_tool_profile)))

    def board_comment(ctx, item_id, text, request_id, quote_comment_id=None):
        return tool_result(lambda: store.mutate("comment", {"item_id": item_id, "text": text,
            "request_id": request_id, "quote_comment_id": quote_comment_id}, agent_actor(ctx, active_tool_profile)))

    tools = (
        ("board_find", board_find, "List authored Work Items in a registered project (default this project). Cross-project IDs are allowed. Dispositions are not execution state.",
         schema({"project_id": STRING, "q": STRING, "disposition": {"enum": ["idea", "planned", "done", "set_aside"]},
                 "archived": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 200}})),
        ("board_get", board_get, "Read an authored item, discussion and informational prerequisite/reverse links; no linked task data is resolved.", schema({"item_id": STRING}, ("item_id",))),
        ("board_capture", board_capture, "Passively capture an Idea. Reuse the exact request_id/arguments on unknown delivery. Does not start or steer work.",
         schema({"project_id": STRING, "title": STRING, "description": STRING, "request_id": STRING}, ("project_id", "title", "request_id"))),
        ("board_update", board_update, "Edit authored item fields using expected_revision CAS; a conflict returns current content. Disposition never controls a run.",
         schema({"item_id": STRING, "expected_revision": {"type": "integer", "minimum": 1}, "request_id": STRING, "changes": CHANGES},
                ("item_id", "expected_revision", "request_id", "changes"))),
        ("board_comment", board_comment, "Append an attributed comment/optional same-item quote. Concurrent appends need only distinct request IDs, not a revision.",
         schema({"item_id": STRING, "text": STRING, "request_id": STRING, "quote_comment_id": {"type": ["string", "null"]}},
                ("item_id", "text", "request_id"))),
    )
    for name, handler, description, tool_schema in tools:
        api.register_tool(name, handler, description=description, schema=tool_schema, timeout_sec=30)

    async def route_read(request, operation):
        def read():
            args = dict(request.query_params)
            if any(len(request.query_params.getlist(key)) != 1 for key in args):
                raise BoardError("invalid", "Query parameters must occur once.")
            if operation == "list":
                exact_keys(args, ("project_id", "q", "disposition", "archived", "offset", "limit"), ("project_id",))
                for key in ("offset", "limit"):
                    if key in args:
                        if len(args[key]) > 19 or not args[key].isascii() or not args[key].isdigit():
                            raise BoardError("invalid", f"{key} must be a nonnegative integer.")
                        args[key] = int(args[key])
                if "archived" in args:
                    if args["archived"] not in ("true", "false"):
                        raise BoardError("invalid", "archived must be true or false.")
                    args["archived"] = args["archived"] == "true"
                return store.find(**args)
            key = "item_id" if operation == "item" else "project_id"
            exact_keys(args, (key,), (key,))
            return store.get(args[key]) if operation == "item" else store.export(args[key])
        payload, status = await asyncio.to_thread(guarded, read)
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    async def route_write(request, operation):
        try:
            # Keep JSON object key identity intact: duplicate keys are invalid,
            # not a silently chosen caller intent. Text fields themselves survive.
            def pairs(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            body = json.loads(await request.body(), object_pairs_hook=pairs,
                              parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (ValueError, UnicodeError, RecursionError):
            return JSONResponse(BoardError("invalid", "A valid JSON object is required.").payload(), status_code=400,
                                headers={"Cache-Control": "no-store"})
        payload, status = await asyncio.to_thread(guarded, lambda: store.mutate(operation, body, INTERFACE_ACTOR))
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    async def list_route(request):
        return await route_read(request, "list")

    async def item_route(request):
        return await route_read(request, "item")

    async def export_route(request):
        return await route_read(request, "export")

    async def capture_route(request):
        return await route_write(request, "capture")

    async def update_route(request):
        return await route_write(request, "update")

    async def comment_route(request):
        return await route_write(request, "comment")

    for name, handler in (("list", list_route), ("item", item_route), ("export", export_route)):
        api.register_route(name, handler=handler, methods=("GET",))
    for name, handler in (("capture", capture_route), ("update", update_route), ("comment", comment_route)):
        api.register_route(name, handler=handler, methods=("POST",))
