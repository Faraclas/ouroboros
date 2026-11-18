"""Thin contextual association tool; project_management owns registry effects."""
from __future__ import annotations

import json

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result


def _project_resources(ctx: ToolContext, action: str, project_id: str,
                       path: str = "", label: str = "", resource_id: str = "",
                       origin: str = "unknown", note: str = "") -> str:
    from ouroboros.project_management import (
        ProjectManagementError, add_project_resource, detach_project_resource,
        list_project_resources,
    )
    from ouroboros.tool_access_paths import canonical_data_root
    from ouroboros.tool_access import active_tool_profile
    from ouroboros.tools.project_journal import _resolve_project_id, _scope_authority

    if action not in {"list", "add", "detach", "footprint", "record", "inspect"}:
        return _publish_tool_result(ctx, ToolResult(
            status="error", code="TOOL_ARG_ERROR", text="action must be list, add, detach, footprint, record or inspect"))
    authority, _metadata = _scope_authority(ctx)
    if action not in {"list", "footprint", "inspect"} and (authority != "root" or active_tool_profile(ctx) in {
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
        readable, observation_error = True, ''
        if action in {"record", "inspect"}:
            import pathlib
            from ouroboros.project_footprint import target_path
            from ouroboros.tools.tool_resolution import _root_containing_absolute_path
            from ouroboros.tool_access import build_resolved_resource_binding
            from ouroboros.protected_artifacts import block_reason_for_path
            selected = str(pathlib.Path(path if action == 'record' else target_path(root, pid, resource_id)).expanduser())
            if action == 'record':
                path = selected
            try:
                physical_root = _root_containing_absolute_path(ctx, "read_file", selected) or "active_workspace"
                binding = build_resolved_resource_binding(ctx, root=physical_root, operation="read", path=selected)
                observation_error = block_reason_for_path(ctx, binding.target_path, "read_bytes", binding) or ''
            except (OSError, ValueError) as exc:
                observation_error = str(exc)
            if observation_error:
                if action == 'inspect':
                    return _publish_tool_result(ctx, ToolResult(status='blocked', code='ACCESS_BLOCKED', text=observation_error))
                readable = False  # an explicit declaration grants no read rights
        if action == "footprint":
            from ouroboros.project_footprint import view, generated_targets
            from ouroboros.project_management import _project
            result = {"project_id": pid, "footprint": {**view(_project(root, pid)), "generated": generated_targets(root, pid)}}
        elif action == "record":
            from ouroboros.project_footprint import record_target
            result = record_target(root, pid, path, task_id=str(ctx.task_id or ""), origin=origin, note=note,
                                   inspect=readable, observation_error=observation_error)
        elif action == "inspect":
            from ouroboros.project_footprint import inspect_target
            result = inspect_target(root, pid, resource_id)
        elif action == "list":
            result = {"project_id": pid, "resources": list_project_resources(root, pid)}
        elif action == "add":
            result = add_project_resource(root, pid, path, label, inspect=False)
        else:
            if not resource_id:
                raise ProjectManagementError("invalid_resource", "resource_id is required")
            result = detach_project_resource(root, pid, resource_id)
        # Resource replies retain their legacy shape, not unrelated authored words.
        result = {key: value for key, value in result.items() if key not in {"description", "notes"}}
    except (OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, ProjectManagementError) else "registry_unavailable"
        return _publish_tool_result(ctx, ToolResult(
            status="error" if isinstance(exc, ProjectManagementError) else "unavailable",
            code="TOOL_ERROR" if isinstance(exc, ProjectManagementError) else "LEGACY_UNAVAILABLE",
            text=f"{reason}: {exc}", meta={"reason": reason}))
    return _publish_tool_result(ctx, ToolResult(
        status="ok", code="OK", text=json.dumps(result, ensure_ascii=False)))


def resource_tool_entries() -> list[ToolEntry]:
    from ouroboros.tools.project_cleanup import cleanup_tool_entries
    return [ToolEntry("project_resources", {
        "name": "project_resources",
        "description": (
            "List, add or detach contextual paths for a Project. Choose this to associate "
            "useful folders/files with a room, not to move work or change cwd. Resources "
            "grant no file access, ownership or locks; existing tool rights still apply. "
            "Ordinary roots may name any registered Project; children may only read their "
            "own room. Detach removes only the association, never files, Git data or tasks. "
            "footprint reads scoped historical observations; record declares one generated or touched target "
            "(including outside cwd), not a recursive inventory. Call before a change for before-facts; "
            "project_created is an attributed agent declaration, not host proof. Unreadable targets may be declared without observation; no rights are granted. Tool add never probes the path. inspect reads one recorded "
            "resource/footprint id for local Git cleanup risks; no fetch, preservation or deletion. "
            "Legacy and opaque shell effects stay unknown; observations confer no deletion authority. Use the separate Safety-checked project_cleanup tool for cleanup planning/execution."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "detach", "footprint", "record", "inspect"]},
            "project_id": {"type": "string"},
            "path": {"type": "string", "description": "Absolute path for add; may be missing."},
            "label": {"type": "string", "default": ""},
            "resource_id": {"type": "string", "description": "Association id for detach, or recorded target id for inspect."},
            "origin": {"type": "string", "enum": ["unknown", "pre_existing", "project_created"], "default": "unknown"},
            "note": {"type": "string", "description": "For record: purpose/before-fact limitations; never secrets or deletion authority.", "default": ""},
        }, "required": ["action", "project_id"], "additionalProperties": False},
    }, _project_resources, safety_policy="skip")] + cleanup_tool_entries()
