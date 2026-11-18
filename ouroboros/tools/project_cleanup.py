"""Safety-checked, ordinary-root surface for explicitly selected cleanup effects."""
from __future__ import annotations

from ouroboros.utils import json_dumps
from functools import partial

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result


def _access(ctx, operation, path):
    from ouroboros.tools.tool_resolution import _root_containing_absolute_path
    from ouroboros.tool_access import build_resolved_resource_binding
    from ouroboros.protected_artifacts import block_reason_for_path
    try:
        name = 'read_file' if operation == 'read' else 'write_file'
        root = _root_containing_absolute_path(ctx, name, path) or 'active_workspace'
        binding = build_resolved_resource_binding(ctx, root=root, operation=operation, path=path)
        return block_reason_for_path(ctx, binding.target_path, 'read_bytes' if operation == 'read' else 'delete', binding) or ''
    except (OSError, ValueError) as exc:
        return str(exc)


def _project_cleanup(ctx: ToolContext, project_id: str, action: str = 'plan', mode: str = 'complete',
                     selections: list | None = None, confirmations: dict | None = None, paths: list | None = None) -> str:
    from ouroboros.project_cleanup import cleanup_plan, cleanup_execute
    from ouroboros.project_management import ProjectManagementError
    from ouroboros.tools.project_journal import _scope_authority, _resolve_project_id
    from ouroboros.tool_access_paths import canonical_data_root
    from ouroboros.tool_access import active_tool_profile
    from ouroboros.body_candidate import serving_repo_dir_for
    try:
        authority, _ = _scope_authority(ctx)
        if authority != 'root' or active_tool_profile(ctx) in {'acting_subagent', 'local_readonly_subagent'}:
            raise ProjectManagementError('cleanup_access_blocked', 'Only an ordinary root may plan or execute cleanup; children report to their parent', 403)
        pid, error = _resolve_project_id(ctx, project_id)
        if error or not pid:
            raise ProjectManagementError('cleanup_access_blocked', error or 'project_id required', 403)
        root, system = canonical_data_root(ctx), serving_repo_dir_for(ctx)
        if action == 'plan':
            result = cleanup_plan(root, pid, mode, system, authorize=partial(_access, ctx, 'read'))
        elif action == 'execute':
            plan = cleanup_plan(root, pid, mode, system, authorize=partial(_access, ctx, 'read'))
            selected = [t for t in plan['targets'] if t['id'] in (selections or [])]
            expected = {p for t in selected for p in (t['path'], t['identity']['resolved_path'])}
            expected.update(t['risk']['facts']['common_dir'] for t in selected if t['risk'].get('arrangement') == 'linked')
            if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths) or set(paths) != expected:
                raise ProjectManagementError('cleanup_paths_required', 'Execute requires paths listing exactly the selected operand/resolved paths (and linked Git common_dir). These explicit paths are shown to Safety; refresh the plan and resubmit.', 409)
            result = cleanup_execute(root, pid, mode, selections, confirmations, 'agent', system,
                                     authorize=partial(_access, ctx, 'write'), read_authorize=partial(_access, ctx, 'read'))
        else:
            raise ProjectManagementError('invalid_cleanup', 'action must be plan or execute')
        return _publish_tool_result(ctx, ToolResult(status='ok', code='OK', text=json_dumps(result, ensure_ascii=False)))
    except (ValueError, OSError) as exc:
        return _publish_tool_result(ctx, ToolResult(status='error', code='TOOL_ERROR', text=str(exc)))


def cleanup_tool_entries():
    return [ToolEntry('project_cleanup', {
        'name': 'project_cleanup',
        'description': 'Plan or execute scoped recorded-target cleanup after a Project is fully closed. Ordinary roots only. No container scan. Plan exposes retained/protected targets, current selection_token and warnings. For execute, paths MUST explicitly list every selected target path and resolved identity path plus the common_dir for linked worktrees; the handler refuses mismatches so Safety sees the actual effects before admission. selections contains recorded target ids, confirmations maps every selected id to its current selection_token (or warning.token). Unique work and unknown coverage require an explicit loss warning and extra confirmation: a human OR agent decides by judgment within authorized scope, not an absolute veto. An agent may supply the token after that judgment. Host protections and physical tool rights still apply; habitat/runtime/shared/live targets are refused. Reopen never restores deleted files; reconstructable repositories are recorded before removal. This is separate from association-only project_resources.detach and is Safety-checked.',
        'parameters': {'type': 'object', 'properties': {
            'project_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['plan', 'execute'], 'default': 'plan'},
            'mode': {'type': 'string', 'enum': ['complete', 'reopen_friendly'], 'default': 'complete'},
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Execute: exact selected paths, resolved paths and linked common_dir, exposed to Safety before effects. Plan may omit.'},
            'selections': {'type': 'array', 'items': {'type': 'string'}},
            'confirmations': {'type': 'object', 'additionalProperties': {'type': 'string'}},
        }, 'required': ['project_id'], 'additionalProperties': False},
    }, _project_cleanup, safety_policy='check')]
