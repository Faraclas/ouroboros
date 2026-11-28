"""Safety-checked, ordinary-root surface for explicitly selected cleanup effects."""
from __future__ import annotations

import contextvars
import os
import pathlib
from functools import partial

from ouroboros.utils import json_dumps
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result

_ACTIVE_COPY_LOADER: contextvars.ContextVar = contextvars.ContextVar('_active_copy_loader', default=None)


def _is_ordinary_root(ctx) -> bool:
    from ouroboros.tool_access import active_tool_profile
    from ouroboros.tools.project_journal import _scope_authority
    authority, _ = _scope_authority(ctx)
    return authority == 'root' and active_tool_profile(ctx) not in {'acting_subagent', 'local_readonly_subagent'}


def _system_repo_light_block(ctx, path) -> str:
    from ouroboros.body_candidate import serving_repo_dir_for
    from ouroboros.config import get_runtime_mode
    from ouroboros.consciousness_authority import effective_runtime_mode
    from ouroboros.tool_access_paths import path_is_relative_to
    if effective_runtime_mode(get_runtime_mode(), getattr(ctx, 'task_metadata', None)) != 'light':
        return ''
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    for attr_path in (serving_repo_dir_for(ctx), getattr(ctx, 'repo_dir', None), getattr(ctx, 'system_repo_dir', None)):
        if attr_path:
            if path_is_relative_to(resolved, pathlib.Path(attr_path).expanduser().resolve(strict=False)):
                return 'light_mode_system_repo_blocked'
    return ''


def _copy_paths_loader(data_root):
    state = {}
    def load():
        if 'result' not in state:
            from ouroboros.project_cleanup_context import copy_records
            try:
                paths = set()
                for row in copy_records(data_root):
                    raw = pathlib.Path(row['path']).expanduser()
                    paths.add(raw.parent.resolve(strict=False) / raw.name)
                    paths.add(raw.resolve(strict=False))
                state['result'] = (frozenset(paths), '')
            except (OSError, ValueError) as exc:
                state['result'] = (frozenset(), str(exc))
        return state['result']
    return load


def _subagent_copy_access(ctx, operation, path, *, copy_loader=None):
    """Scoped access for recorded Viper copies under OUROBOROS_SUBAGENT_WORKTREE_ROOT, or None if elsewhere."""
    from ouroboros.config import get_subagent_worktree_root
    from ouroboros.protected_artifacts import block_reason_for_path
    from ouroboros.tool_access_paths import canonical_data_root, path_is_relative_to
    from ouroboros import unindexed
    try:
        raw_root = get_subagent_worktree_root()
        if not raw_root:
            return None
        worktree_root = pathlib.Path(raw_root).expanduser().resolve(strict=False)
        candidate = pathlib.Path(path).expanduser()
        if not candidate.is_absolute():
            return None
        operand = candidate.parent.resolve(strict=False) / candidate.name
        if not path_is_relative_to(operand, worktree_root):
            return None
    except (OSError, ValueError):
        return None
    if not _is_ordinary_root(ctx):
        if operation == 'read':
            from ouroboros.tool_access import build_resolved_resource_binding
            try:
                build_resolved_resource_binding(ctx, root='active_workspace', operation='read', path=str(candidate))
                return None  # Child's own active_workspace retains ordinary read_bytes inspection.
            except (OSError, ValueError):
                pass
        return 'Only an ordinary root may inspect or retire Viper working copies'
    copies_dir = (worktree_root / unindexed.COPIES).resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if operand == worktree_root or resolved == worktree_root or operand == copies_dir or resolved == copies_dir:
        return f'Viper worktree container {operand} cannot be inspected or deleted as a target'
    if not path_is_relative_to(resolved, worktree_root):
        return f'Symlink target {resolved} escapes subagent_worktrees root {worktree_root}'
    loader = copy_loader or _copy_paths_loader(canonical_data_root(ctx))
    recorded_paths, load_error = loader()
    if load_error:
        return load_error
    is_recorded = operand in recorded_paths or resolved in recorded_paths
    if not is_recorded and not (not os.path.lexists(operand) and path_is_relative_to(operand, copies_dir)):
        return f'Unrecorded path {operand} inside subagent_worktrees cannot bypass copy custody'
    return block_reason_for_path(ctx, resolved, 'read_bytes' if operation == 'read' else 'delete') or ''


def _common_dir_access(ctx, common_dir):
    from ouroboros.protected_artifacts import block_reason_for_path
    return _system_repo_light_block(ctx, common_dir) or (
        block_reason_for_path(ctx, pathlib.Path(common_dir).expanduser().resolve(strict=False), 'delete') or ''
    )


def _access(ctx, operation, path):
    from ouroboros.tools.tool_resolution import _root_containing_absolute_path
    from ouroboros.tool_access import build_resolved_resource_binding
    from ouroboros.tool_access_paths import canonical_data_root
    from ouroboros.protected_artifacts import block_reason_for_path
    if operation == 'write':
        light_block = _system_repo_light_block(ctx, path)
        if light_block:
            return light_block
    copy_loader = _ACTIVE_COPY_LOADER.get() or _copy_paths_loader(canonical_data_root(ctx))
    copy_verdict = _subagent_copy_access(ctx, operation, path, copy_loader=copy_loader)
    if copy_verdict is not None:
        return copy_verdict
    try:
        name = 'read_file' if operation == 'read' else 'write_file'
        root = _root_containing_absolute_path(ctx, name, path) or 'active_workspace'
        binding = build_resolved_resource_binding(ctx, root=root, operation=operation, path=path)
        return block_reason_for_path(ctx, binding.target_path, 'read_bytes' if operation == 'read' else 'delete', binding) or ''
    except (OSError, ValueError) as exc:
        return str(exc)


def _project_cleanup(ctx: ToolContext, project_id: str, action: str = 'plan', mode: str = 'complete',
                     selections: list | None = None, confirmations: dict | None = None, paths: list | None = None,
                     offset: int = 0, limit: int = 25, query: str = '', plan_sha256: str = '') -> str:
    from ouroboros.project_cleanup import cleanup_plan, cleanup_plan_page, cleanup_execute
    from ouroboros.project_management import ProjectManagementError
    from ouroboros.tools.project_journal import _resolve_project_id
    from ouroboros.tool_access_paths import canonical_data_root
    from ouroboros.body_candidate import serving_repo_dir_for
    try:
        if not _is_ordinary_root(ctx):
            raise ProjectManagementError('cleanup_access_blocked', 'Only an ordinary root may plan or execute cleanup; children report to their parent', 403)
        pid, error = _resolve_project_id(ctx, project_id)
        if error or not pid:
            raise ProjectManagementError('cleanup_access_blocked', error or 'project_id required', 403)
        root, system = canonical_data_root(ctx), serving_repo_dir_for(ctx)
        token = _ACTIVE_COPY_LOADER.set(_copy_paths_loader(root))
        try:
            read_auth = partial(_access, ctx, 'read')
            write_auth = partial(_access, ctx, 'write')
            if action == 'plan':
                result = cleanup_plan_page(root, pid, mode, system, authorize=read_auth,
                                           offset=offset, limit=limit, query=query, plan_sha256=plan_sha256)
            elif action == 'execute':
                plan = cleanup_plan(root, pid, mode, system, authorize=read_auth, selected_ids=set(selections or []))
                selected = [t for t in plan['targets'] if t['id'] in (selections or [])]
                expected = {p for t in selected for p in (t['path'], t['identity']['resolved_path'])}
                expected.update(t['risk']['facts']['common_dir'] for t in selected if t['risk'].get('arrangement') == 'linked')
                if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths) or set(paths) != expected:
                    raise ProjectManagementError('cleanup_paths_required', 'Execute requires paths listing exactly the selected operand/resolved paths (and linked Git common_dir). These explicit paths are shown to Safety; refresh the plan and resubmit.', 409)
                result = cleanup_execute(root, pid, mode, selections, confirmations, 'agent', system,
                                         authorize=write_auth, read_authorize=read_auth,
                                         common_dir_authorize=partial(_common_dir_access, ctx))
            else:
                raise ProjectManagementError('invalid_cleanup', 'action must be plan or execute')
        finally:
            _ACTIVE_COPY_LOADER.reset(token)
        return _publish_tool_result(ctx, ToolResult(status='ok', code='OK', text=json_dumps(result, ensure_ascii=False)))
    except (ValueError, OSError) as exc:
        return _publish_tool_result(ctx, ToolResult(status='error', code='TOOL_ERROR', text=str(exc)))


def cleanup_tool_entries():
    return [ToolEntry('project_cleanup', {
        'name': 'project_cleanup',
        'description': 'Plan or execute scoped recorded-target cleanup after a Project is fully closed. Ordinary roots only. No container scan. Plan returns bounded current pages (default25, max100), exact total/matched/structurally-protected counts and next_offset. Unconditionally-protected internal runtime/artifact paths under the data root are omitted from paged rows and counted in omitted_internal_total (managed in Dashboard -> Storage -> Temporary space, never removable here). Use offset/query and the returned plan_sha256 to inspect every target without mixing changed facts. A page is not the whole plan; unseen targets are not selected. Plan exposes recorded targets, selection_token and warnings; caller-inaccessible targets stay explicitly retained and loss-uninspected, with the physical denial explained. Protected targets are loss-uninspected, not known clean. For execute, paths MUST explicitly list every selected target path and resolved identity path plus the common_dir for linked worktrees; the handler refuses mismatches so Safety sees the actual effects before admission. selections contains recorded target ids, confirmations maps every selected id to its current selection_token (or warning.token). Unique work and unknown coverage require an explicit loss warning and extra confirmation: a human OR agent decides by judgment within authorized scope, not an absolute veto. An agent may supply the token after that judgment. Host protections and physical tool rights still apply, except ordinary roots may inspect and retire recorded subagent_worktrees copies (and matching common_dir) via retire_copy while light mode blocks serving .git writes; habitat/runtime/shared/live targets are refused. Reopen never restores deleted files; reconstructable repositories are recorded before removal. This is separate from association-only project_resources.detach and is Safety-checked.',
        'parameters': {'type': 'object', 'properties': {
            'project_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['plan', 'execute'], 'default': 'plan'},
            'mode': {'type': 'string', 'enum': ['complete', 'reopen_friendly'], 'default': 'complete'},
            'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 25},
            'query': {'type': 'string', 'description': 'Literal path filter; totals distinguish matches from complete scope.'},
            'plan_sha256': {'type': 'string', 'description': 'Pass the prior page digest on subsequent current rereads; changed facts refuse with cleanup_plan_changed.'},
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Execute: exact selected paths, resolved paths and linked common_dir, exposed to Safety before effects. Plan may omit.'},
            'selections': {'type': 'array', 'items': {'type': 'string'}},
            'confirmations': {'type': 'object', 'additionalProperties': {'type': 'string'}},
        }, 'required': ['project_id'], 'additionalProperties': False},
    }, _project_cleanup, safety_policy='check', result_selection={"arguments": ["limit", "offset"]})]
