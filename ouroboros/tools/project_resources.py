"""Thin contextual association tool; project_management owns registry effects."""
from __future__ import annotations

import json

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result


def _project_resources(ctx: ToolContext, action: str, project_id: str,
                       path: str = "", label: str = "", resource_id: str = "") -> str:
    from ouroboros.project_management import (
        ProjectManagementError, add_project_resource, detach_project_resource,
        list_project_resources,
    )
    from ouroboros.tool_access_paths import canonical_data_root
    from ouroboros.tool_access import active_tool_profile
    from ouroboros.tools.project_journal import _resolve_project_id, _scope_authority

    if action not in {"list", "add", "detach"}:
        return _publish_tool_result(ctx, ToolResult(
            status="error", code="TOOL_ARG_ERROR", text="action must be list, add or detach"))
    authority, _metadata = _scope_authority(ctx)
    if action != "list" and (authority != "root" or active_tool_profile(ctx) in {
            "acting_subagent", "local_readonly_subagent"}):
        return _publish_tool_result(ctx, ToolResult(
            status="blocked", code="ACCESS_BLOCKED",
            text="Only an ordinary root may change project resource associations; children report to their parent."))
    pid, scope_error = _resolve_project_id(ctx, project_id)
    if scope_error or not pid:
        return _publish_tool_result(ctx, ToolResult(
            status="blocked" if scope_error and "FORBIDDEN" in scope_error else "error",
            code="ACCESS_BLOCKED" if scope_error and "FORBIDDEN" in scope_error else "TOOL_ARG_ERROR",
            text=scope_error or "project_id is required"))
    try:
        root = canonical_data_root(ctx)
        if action == "list":
            result = {"project_id": pid, "resources": list_project_resources(root, pid)}
        elif action == "add":
            result = add_project_resource(root, pid, path, label)
        else:
            if not resource_id:
                raise ProjectManagementError("invalid_resource", "resource_id is required")
            result = detach_project_resource(root, pid, resource_id)
    except (OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, ProjectManagementError) else "registry_unavailable"
        return _publish_tool_result(ctx, ToolResult(
            status="error" if isinstance(exc, ProjectManagementError) else "unavailable",
            code="TOOL_ERROR" if isinstance(exc, ProjectManagementError) else "LEGACY_UNAVAILABLE",
            text=f"{reason}: {exc}", meta={"reason": reason}))
    return _publish_tool_result(ctx, ToolResult(
        status="ok", code="OK", text=json.dumps(result, ensure_ascii=False)))


def resource_tool_entries() -> list[ToolEntry]:
    return [ToolEntry("project_resources", {
        "name": "project_resources",
        "description": (
            "List, add or detach contextual paths for a Project. Choose this to associate "
            "useful folders/files with a room, not to move work or change cwd. Resources "
            "grant no file access, ownership or locks; existing tool rights still apply. "
            "Ordinary roots may name any registered Project; children may only list their "
            "own room. Detach removes only the association, never files, Git data or tasks."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "detach"]},
            "project_id": {"type": "string"},
            "path": {"type": "string", "description": "Absolute path for add; may be missing."},
            "label": {"type": "string", "default": ""},
            "resource_id": {"type": "string", "description": "Association id for detach."},
        }, "required": ["action", "project_id"], "additionalProperties": False},
    }, _project_resources, safety_policy="skip")]
