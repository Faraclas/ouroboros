"""Physical protection, process observations and retirement adapters for cleanup."""
from __future__ import annotations

import os
import json
import pathlib
import re
from urllib.parse import urlsplit, urlunsplit

from ouroboros import projects_registry as registry, task_store
from ouroboros.process_identity import process_kernel_birth

NESTED_SCAN_DEPTH = 8
NESTED_SCAN_ENTRIES = 4096

MANAGED_PROJECT_IDS = frozenset({'viper-interface', 'viper-discord', 'viper-model-transport'})


def lease_live(lease):
    if not isinstance(lease, dict) or not lease.get('pid'):
        return False
    try:
        pid = int(lease['pid']); os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (ValueError, OSError):
        return True  # unknown ownership is not proven dead
    birth = process_kernel_birth(pid)
    return not birth or not lease.get('birth') or birth == lease['birth']


def lease(operation):
    return {'id': operation, 'pid': os.getpid(), 'birth': process_kernel_birth(os.getpid())}


def fixed_roots(drive_root, system_repo_dir):
    """The distinction matters: home/root are forbidden ancestors, not subtrees."""
    from ouroboros.config import get_subagent_worktree_root, get_deliverables_root, get_subagent_projects_root
    return [(pathlib.Path(p).expanduser().resolve(), inside) for p, inside in (
        ('/', False), (pathlib.Path.home(), False), (drive_root, True), (system_repo_dir, True),
        (get_subagent_worktree_root(), False), (get_deliverables_root(), False), (get_subagent_projects_root(), False))]


def mount_paths():
    """One kernel inventory at execution, not a recursive filesystem probe."""
    text = pathlib.Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    return [pathlib.Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4]))
            for line in text.splitlines()]


def contains_mount(path, mounts):
    target = pathlib.Path(path)
    return any(target == mount or target in mount.parents for mount in mounts)


def remote_url(value):
    """Never retain URL or SCP-style userinfo as reconstruction credentials."""
    parts = urlsplit(value)
    if parts.scheme and parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
    # SCP syntax permits arbitrary userinfo too; do not retain credentials.
    return value.rsplit('@', 1)[-1] if '@' in value and ':' in value else value


def process_cwds():
    """Readable kernel cwd links only; unrelated unreadable processes are not ours."""
    try:
        processes = pathlib.Path('/proc').iterdir()
        return [cwd for proc in processes if proc.name.isdecimal()
                for cwd in _read_cwd(proc)]
    except OSError:
        return []


def _read_cwd(proc):
    try:
        return [str((proc / 'cwd').resolve(strict=True))]
    except (OSError, RuntimeError):
        return []


def repository_dependency(path, risk, git):
    """Bounded metadata-only nested-repo search, never following directory links."""
    if pathlib.Path(path).is_symlink():
        return ''  # unlinking a link cannot erase its target's Git metadata
    arrangement = risk.get('arrangement')
    if arrangement in {'main', 'bare'}:
        trees = risk.get('facts', {}).get('worktrees')
        if not trees:
            return 'git_worktree_inventory_unknown'
        if len(trees) > 1:
            return 'other_git_worktrees_depend_on_repository'
    if risk['status'] != 'not_repository':
        return ''
    seen = 0
    try:
        def failed(error):
            raise error
        for folder, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            seen += len(dirs) + len(files) + 1
            if seen > NESTED_SCAN_ENTRIES:
                return 'nested_repository_inventory_unknown'
            depth = len(pathlib.Path(folder).relative_to(path).parts)
            if '.git' in dirs or '.git' in files:
                raw = git(folder, 'worktree', 'list', '--porcelain')
                trees = [line[9:] for line in raw.splitlines() if line.startswith('worktree ')]
                if not trees:
                    return 'git_worktree_inventory_unknown'
                # A nested linked checkout needs its Git owner to unregister it.
                if '.git' in files:
                    return 'nested_linked_worktree_requires_repository_owner'
                target = pathlib.Path(path).resolve()
                if any(not pathlib.Path(t).resolve().is_relative_to(target) for t in trees):
                    return 'other_git_worktrees_depend_on_repository'
                if '.git' in dirs:
                    dirs.remove('.git')
            dirs[:] = [d for d in dirs if not (pathlib.Path(folder) / d).is_symlink()]
            if depth >= NESTED_SCAN_DEPTH and dirs:
                return 'nested_repository_inventory_unknown'
        return ''
    except Exception:
        return 'nested_repository_inventory_unknown'


def copy_records(root):
    """Strict inspection without the ordinary list owner's legacy-import writes."""
    from ouroboros.subagent_worktrees import _records_dir, _legacy_registry_path
    records = []
    legacy = _legacy_registry_path(root)
    try:
        value = json.loads(legacy.read_text(encoding='utf-8'))
    except FileNotFoundError:
        value = []
    records = value.get('worktrees') if isinstance(value, dict) else value
    if not isinstance(records, list):
        raise ValueError('Legacy copy inventory is unreadable')
    try:
        files = list(_records_dir(root).iterdir())
    except FileNotFoundError:
        files = []
    current = [json.loads(p.read_text(encoding='utf-8')) for p in files if p.suffix == '.json' and not p.name.startswith('.')]
    if any(not isinstance(r, dict) or not isinstance(r.get('path'), str) or not pathlib.Path(r['path']).is_absolute() for r in [*records, *current]):
        raise ValueError('Copy inventory has an unknown path or owner')
    # Current per-copy observations supersede their legacy predecessor.
    return list({r['path']: r for r in [*records, *current]}.values())


def context(root, project_id):
    from ouroboros.body_candidate import live_lineage_processes
    bindings = registry._load_bindings(root, strict=True)['bindings']
    bound = {tid for tid, b in bindings.items() if isinstance(b, dict) and b.get('project_id') == project_id}
    tids = set(task_store.newest_ids(root, where='project_id=?', params=(project_id,))) | bound
    # A moved task's old facts are not current Project ownership.
    tids = {tid for tid in tids if not isinstance(bindings.get(tid), dict) or bindings[tid].get('project_id') == project_id}
    processes = any(live_lineage_processes(root, tid) for tid in tids)
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES
    terminal = sorted(_TRULY_TERMINAL_STATUSES)
    live_ids = task_store.newest_ids(root, where=f"status NOT IN ({','.join('?' for _ in terminal)})", params=tuple(terminal))
    live = task_store.documents_by_ids(root, live_ids)
    workspaces = [str(t[key]) for t in live.values() for key in ('workspace_root', 'cwd', 'working_dir') if t.get(key)]
    copies = copy_records(root)  # no migration/report writes or skipped corrupt rows
    return {'task_ids': tids, 'processes': processes, 'workspaces': workspaces,
            'copies': copies, 'bindings': bindings, 'process_cwds': process_cwds()}


def retire_copy(root, row):
    """Only a recorded owner's existing retirement protocol may erase its checkout."""
    from ouroboros import body_candidate, subagent_worktrees as wt
    if row.get('snapshot_id'):
        if not wt.snapshot_released(row, set(), data_dir=root):
            return False
        return wt.remove_execution_snapshot(row['snapshot_id'], data_dir=root)
    if row.get('kind') == 'body_candidate':
        verdict = body_candidate.retention_verdict(row, expired=True, data_dir=root)
        if not verdict['remove'] or not body_candidate.verdict_holds(row, verdict, data_dir=root):
            return False
        branch = '' if verdict.get('keep_branch') else str(row.get('branch') or '')
    else:
        from supervisor.queue import task_settlement_liveness
        if not wt._task_proven_over(str(row.get('task_id') or ''), root, task_settlement_liveness):
            return False
        branch = str(row.get('branch') or '')
    return wt._retire_worktree(row, wt._resolve_root(None), root, branch)


def _broadcast(project):
    from supervisor.queue_transitions import _broadcast_projects_changed
    _broadcast_projects_changed(project['id'], project.get('chat_id'))


def clear_pending_cleanup(root, project_id):
    """Owner recovery action: cancel a queued request, never interrupt a live effect."""
    from ouroboros.project_management import ProjectManagementError
    from ouroboros.utils import utc_now_iso
    with registry._file_write_lock(registry._registry_path(root)):
        data = registry._load(root, strict=True)
        project = next((p for p in data['projects'] if p['id'] == project_id), None)
        if project is None:
            raise ProjectManagementError('project_not_found', 'Unknown Project', 404)
        if project_id in MANAGED_PROJECT_IDS:
            raise ProjectManagementError('managed_project', 'Managed habitat Projects are excluded from generic cleanup', 403)
        if lease_live(project.get('cleanup_lease')):
            raise ProjectManagementError('cleanup_in_progress', 'A cleanup process may still be running; wait for its outcome or stop its owning process before clearing the request', 409)
        request = project.get('cleanup_request')
        if request and request.get('status') in {'pending', 'deferred'}:
            project['cleanup_request'] = {**request, 'status': 'cancelled', 'finished_at': utc_now_iso(),
                                          'result': {'reason': 'Saved cleanup cancelled by owner; no files removed by this action'}}
        project.pop('cleanup_lease', None)
        registry._save(root, data)
    _broadcast(project)
    return {'id': 'cancelled-request', 'phase': 'cancelled', 'at': utc_now_iso(), 'project_id': project_id,
            'mode': (request or {}).get('mode', 'complete'), 'decided_by': 'human', 'results': [],
            'ok': True, 'cleanup_accepted': False, 'folder_untouched': True}


def run_pending_cleanup(root, project_id):
    """Settle every saved request, including adapter failures and live-lease deferral."""
    from ouroboros.project_cleanup import cleanup_execute
    from ouroboros.utils import utc_now_iso
    with registry._file_write_lock(registry._registry_path(root)):
        data = registry._load(root, strict=True)
        project = next(p for p in data['projects'] if p['id'] == project_id)
        request = project.get('cleanup_request')
        if not request or request.get('status') not in {'pending', 'deferred'}:
            return
        request = dict(request)
        busy = lease_live(project.get('cleanup_lease'))
        if busy and request.get('status') == 'deferred':
            return  # already explained; the existing resume owner retries when the lease dies
    status = 'deferred' if busy else 'finished'
    try:
        result = ({'error': 'A cleanup lease is still live or unknown. Wait for the owning process, then retry cleanup or POST action=clear_pending to the cleanup endpoint.'} if busy else
                  cleanup_execute(root, project_id, request['mode'], request['selections'],
                                  request['confirmations'], 'human', request['system_repo_dir'], pending_request=request))
    except Exception as exc:
        status = 'failed'
        result = {'error': f'{type(exc).__name__}: {exc}'}
    if not busy and result.get('error'):
        status = 'failed'
    with registry._file_write_lock(registry._registry_path(root)):
        data = registry._load(root, strict=True)
        project = next(p for p in data['projects'] if p['id'] == project_id)
        if project.get('cleanup_request') == request:
            project['cleanup_request'] = {**request, 'status': status, 'finished_at': utc_now_iso(), 'result': result}
            registry._save(root, data)
    _broadcast(project)
