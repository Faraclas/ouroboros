"""Explicit recorded-target cleanup after the existing Project Close fence.

The registry lock serializes cleanup with Reopen and resource edits. No watcher,
container census or implicit ownership follows a room association. History is
write-ahead: an interrupted attempt remains unknown, never reported deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import uuid

from ouroboros import projects_registry as registry, project_footprint as footprint
from ouroboros.git_observation import host_git_env
from ouroboros.platform_layer import subprocess_hidden_kwargs
from ouroboros.project_management import (
    ProjectManagementError, _preview_identity, _preview_overlap, resources_for_project,
)
from ouroboros.repository_risk import inspect_repository
from ouroboros.utils import utc_now_iso

from ouroboros import project_cleanup_context as custody
from ouroboros.project_cleanup_context import MANAGED_PROJECT_IDS
MODES = frozenset({'complete', 'reopen_friendly'})


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _paths(row):
    values = [*resources_for_project(row), *footprint.entries(row)]
    if row.get('working_dir'):
        values.append({'path': row['working_dir'], 'source': 'starting_folder'})
    return values


def _git(path, *args):
    env = host_git_env({k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
    env.update(GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(path), *args],
                            capture_output=True, timeout=30, env=env, **subprocess_hidden_kwargs())
    if result.returncode:
        raise OSError(f'git {args[0]} failed (exit {result.returncode})')
    return os.fsdecode(result.stdout).removesuffix('\n')


def _risk(path, identity, cached=None):
    """Current Git counts plus change identity; no timestamp in confirmation token."""
    risk = dict(cached) if cached is not None else inspect_repository(str(path))
    risk.pop('observed_at', None)
    if cached is None:
        risk['cleanup_retain_reason'] = custody.repository_dependency(path, risk, _git)
        if risk['status'] in {'repository', 'partial'}:
            try:
                risk['cleanup_remote'] = custody.remote_url(_git(path, 'remote', 'get-url', 'origin'))
            except (OSError, subprocess.SubprocessError):
                risk['cleanup_remote'] = None
    facts = risk.get('facts', {})
    unique = any(facts.get(key) for key in ('uncommitted', 'untracked', 'ignored', 'stashes', 'commits_not_in_remote_tracking'))
    unknown = risk['status'] not in {'repository', 'missing'} or bool(risk['gaps'])
    # Non-repository directories can contain opaque/nested work. An explicit
    # producer-recorded creation permits normal temp cleanup, not backup claims.
    warning = None
    if unique or unknown:
        warning = {'message': 'Local changes, untracked/ignored files, stashes, unpushed commits or unknown inspection coverage may be lost. Review the current facts and explicitly confirm this target.',
                   'token': _hash([str(path), identity, risk]), 'unique_work': bool(unique), 'unknown': bool(unknown)}
    return risk, warning


def _plan(drive_root, project_id, mode, system_repo_dir, projects, authorize=None, risk_cache=None):
    if mode not in MODES:
        raise ProjectManagementError('invalid_cleanup', 'mode must be complete or reopen_friendly')
    project = next((p for p in projects if p['id'] == project_id), None)
    if project is None:
        raise ProjectManagementError('project_not_found', 'Unknown Project', 404)
    if project_id in MANAGED_PROJECT_IDS:
        raise ProjectManagementError('managed_project', 'Managed habitat Projects are excluded from generic cleanup', 403)
    if not system_repo_dir:
        raise ProjectManagementError('cleanup_context_missing', 'The system repository root is required')
    # Resolution is part of the physical access check; denied targets receive
    # spelling/alias facts only, never stat, Git or nested-content inspection.
    identities, denials = {}, {}
    def identity(raw):
        raw = str(raw)
        if raw not in identities:
            denied = authorize(raw) if authorize else ''
            denials[raw] = denied
            identities[raw] = ({'resolved_path': str(pathlib.Path(raw).expanduser().resolve(strict=False)),
                                'identity_status': 'access_denied', 'inode': None}
                               if denied else _preview_identity(raw))
        return identities[raw]
    protected = []
    facts_context = custody.context(drive_root, project_id)
    others, closed_others = [], []
    for row in projects:
        for item in _paths(row):
            observed = identity(item['path'])
            if row['id'] in MANAGED_PROJECT_IDS:
                protected.append(observed)
            if row['id'] != project_id:
                (closed_others if row['lifecycle'] == registry.PROJECT_TOMBSTONED else others).append(observed)
    generated = footprint.generated_targets(drive_root, project_id)
    grouped = {}
    for entry in [*_paths(project), *generated['targets']]:
        path = pathlib.Path(entry['path']).expanduser()
        if not path.is_absolute():
            raise ProjectManagementError('invalid_cleanup', 'Recorded cleanup paths must be absolute')
        observed = dict(identity(str(path)))
        denied = denials[str(path)]
        observed['operand_inode'] = None
        if not denied:
            try:
                info = path.lstat()
                observed['operand_inode'] = (info.st_dev, info.st_ino)
            except FileNotFoundError:
                pass
        # Never turn a symlink target into the deletion operand. Its resolved
        # identity still participates in all protection/alias comparisons.
        operand = str(path.parent.resolve() / path.name)
        key = operand if denied or path.is_symlink() else observed['resolved_path']
        group = grouped.setdefault(key, {'path': operand, 'identity': observed, 'sources': [], 'access_denial': denied})
        group['sources'].append(entry)
        if denied:
            # Parent-directory aliases may share an operand. No entry order may
            # erase a denied reference when their sources are coalesced.
            group['access_denial'] = denied
            group['identity'] = observed
    fixed_identities = [(identity(str(p)), inside) for p, inside in custody.fixed_roots(drive_root, system_repo_dir)]
    copy_identities = [(copy, identity(copy['path'])) for copy in facts_context['copies']]
    workspace_identities = [identity(p) for p in facts_context['workspaces']]
    targets = []
    for item in grouped.values():
        path, observed, sources = pathlib.Path(item['path']), item['identity'], item['sources']
        origins = {s.get('origin', 'unknown') for s in sources}
        sources_set = {s.get('source') for s in sources}
        created = any(s.get('source') in {'genesis_producer', 'clone_producer', 'create_producer'}
                      and s.get('origin') in {'project_created', 'genesis', 'cloned'} for s in sources)
        temp = 'private_copy_record' in sources_set
        output = 'recorded_output' in origins
        kind = 'recorded_output' if output else 'project_created' if created else 'generated_temp' if temp else 'attached_external'
        protection = 'runtime_or_habitat_overlap' if any(_preview_overlap(observed, p) for p in protected) else ''
        for fixed, inside in fixed_identities:
            relation = _preview_overlap(observed, fixed)
            if relation in ({'same', 'contains', 'inside'} if inside else {'same', 'contains'}):
                protection = 'runtime_or_habitat_overlap'
        if sources_set & {'task.child_drive_root', 'task.headless_child_drive_root', 'task.artifacts'}:
            protection = 'runtime_memory_or_recorded_artifact'
        if 'task.workspace_root' in sources_set and not created and not temp:
            protection = 'workspace_association_not_disposable'
        copy_record = None
        if not protection:
            for copy, copy_identity in copy_identities:
                relation = _preview_overlap(observed, copy_identity)
                owner = copy.get('task_id')
                owned = owner in facts_context['task_ids'] or owner == f'project_{project_id}'
                if relation == 'same' and owned:
                    copy_record = copy
                elif relation:
                    protection = 'another_copy_or_copy_ancestor'
        if facts_context['processes'] or any(_preview_overlap(observed, p) for p in workspace_identities):
            protection = 'live_task_or_process_uses_target'
        if not protection and any(_preview_overlap(observed, p) for p in others):
            protection = 'shared_with_another_project'
        if any(pathlib.Path(cwd).is_relative_to(observed['resolved_path']) for cwd in facts_context['process_cwds']):
            protection = protection or 'process_cwd_inside'
        cached = None if risk_cache is None else risk_cache.get(item['path'], {
            'status': 'unavailable', 'gaps': ['New target requires inspection outside the registry lock'],
            'facts': {}, 'cleanup_retain_reason': 'new_target_requires_inspection'})
        if item['access_denial']:
            protection = 'caller_access_denied'
        # Loss inspection cannot make an already-protected target removable.
        # Report it as uninspected, and inspect afresh if that protection ends.
        if protection:
            risk = {'status': 'not_inspected', 'facts': {},
                    'gaps': ['Loss inspection not performed: ' + protection],
                    'cleanup_retain_reason': protection}
            warning = None
        else:
            risk, warning = _risk(path, observed, cached)
        if not warning and not (created or temp) and risk['status'] not in {'missing'}:
            warning = {'message': 'This existing or unknown-origin target may contain unique local material. Association is not disposability. Explicitly confirm its loss.',
                       'token': _hash([str(path), observed, risk]), 'unique_work': False, 'unknown': True}
        if any(_preview_overlap(observed, p) for p in closed_others):
            warning = {'message': 'Another closed Project still references this path. Explicitly confirm removing its local material.',
                       'token': _hash([str(path), observed, risk, 'closed_shared']), 'unique_work': True, 'unknown': True}
        exists = None if item['access_denial'] else path.exists() or path.is_symlink()
        reconstruction = None
        retain_reason = protection or risk.get('cleanup_retain_reason', '')
        facts = risk.get('facts', {})
        if risk.get('arrangement') == 'inside_repository':
            retain_reason = retain_reason or 'inside_repository_requires_repository_owner'
        if path.name == '.git' or '.git' in path.parts:
            retain_reason = retain_reason or 'git_metadata_requires_repository_owner'
        if mode == 'reopen_friendly':
            if output:
                retain_reason = retain_reason or 'recorded_output_kept'
            elif risk['status'] in {'repository', 'partial'}:
                try:
                    remote = risk.get('cleanup_remote')
                    if not remote or not facts.get('head') or not facts.get('branch'):
                        raise OSError('Remote, branch and HEAD are required')
                    reconstruction = {'remote_url': custody.remote_url(remote), 'branch': facts['branch'], 'head': facts['head'],
                                      'remote_coverage': risk['remote_coverage']}
                except (OSError, subprocess.SubprocessError):
                    retain_reason = retain_reason or 'reconstruction_unavailable'
        target_id = 'cleanup:' + _hash(item['path'])
        targets.append({'id': target_id, 'path': item['path'], 'kind': kind,
                        'origins': sorted(origins), 'sources': sources, 'identity': observed,
                        'exists': exists, 'protection': protection, 'retain_reason': retain_reason,
                        'access_denial': item['access_denial'],
                        'risk': risk, 'warning': warning, 'reconstruction': reconstruction, 'copy_record': copy_record,
                        'selection_token': _hash([target_id, observed, risk]), 'operand_inode': observed['operand_inode'],
                        'default_selected': bool((created or temp) and not output and not retain_reason and
                                                 (mode == 'complete' or (reconstruction and not warning))),
                        'action': 'retain' if retain_reason else 'remove_if_selected'})
    return {'project_id': project_id, 'mode': mode, 'lifecycle': project['lifecycle'],
            'targets': targets, 'gaps': [*generated['gaps'], *(['Some recorded targets are retained without loss inspection because caller access is denied.'] if any(t['access_denial'] for t in targets) else [])],
            'coverage': 'Recorded targets only. No container inventory or backup verification. Remote refs are local knowledge, not a live push check.'}


def cleanup_plan(drive_root, project_id, mode='complete', system_repo_dir=None, *, authorize=None):
    """Read-only explicit inspection. Association alone never selects deletion."""
    return _plan(drive_root, project_id, mode, system_repo_dir,
                 registry._load(drive_root, strict=True)['projects'], authorize)


def _remove(target):
    path = pathlib.Path(target['path'])
    if path.is_symlink():
        path.unlink()
    elif target['risk'].get('arrangement') == 'linked':
        # The enclosing repository owns linked-checkout registration. Locked
        # worktrees remain a refusal from Git, never silently unlocked here.
        _git(target['risk']['facts']['common_dir'], 'worktree', 'remove', '--force', str(path))
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def cleanup_execute(drive_root, project_id, mode, selections, confirmations, decided_by,
                    system_repo_dir=None, *, authorize=None, read_authorize=None, pending_request=None):
    """Replan, confirm current warnings, persist intent, then remove selected paths.

    authorize is the tool caller's existing physical read/write authority check;
    browser requests already originate at the authenticated owner boundary.
    """
    if decided_by not in {'human', 'agent'} or not isinstance(selections, list) or any(not isinstance(s, str) for s in selections):
        raise ProjectManagementError('invalid_cleanup', 'decided_by and a list of target ids are required')
    if not isinstance(confirmations, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in confirmations.items()):
        raise ProjectManagementError('invalid_cleanup', 'confirmations must map target ids to current warning tokens')
    inspected = cleanup_plan(drive_root, project_id, mode, system_repo_dir, authorize=read_authorize)
    risk_cache = {t['path']: t['risk'] for t in inspected['targets']}
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True)
        project = next((p for p in data['projects'] if p['id'] == project_id), None)
        if not project or project['lifecycle'] != registry.PROJECT_TOMBSTONED:
            raise ProjectManagementError('project_not_closed', 'Wait until the Project is fully closed before cleanup', 409)
        if project.get('cleanup_request', {}).get('status') in {'pending', 'deferred'} and project['cleanup_request'] != pending_request:
            raise ProjectManagementError('cleanup_in_progress', 'The saved Close cleanup is pending; wait for its outcome or POST action=clear_pending to the cleanup endpoint', 409)
        plan = _plan(drive_root, project_id, mode, system_repo_dir, data['projects'], read_authorize, risk_cache)
        targets = {t['id']: t for t in plan['targets']}
        if set(selections) - targets.keys():
            raise ProjectManagementError('cleanup_plan_changed', 'Unknown target id; refresh the cleanup plan', 409)
        reasons = {}
        for tid, target in targets.items():
            reason = target['retain_reason'] or ('not_selected' if tid not in selections else '')
            if not reason and target['exists'] and confirmations.get(tid) != (target['warning']['token'] if target['warning'] else target['selection_token']):
                reason = 'current_warning_confirmation_required' if target['warning'] else 'current_selection_confirmation_required'
            if not reason and authorize:
                reason = authorize(target['path']) or ''
                if not reason and target['risk'].get('arrangement') == 'linked':
                    reason = authorize(target['risk']['facts']['common_dir']) or ''
            reasons[tid] = reason
        # A selected parent must not swallow a retained child, including a child
        # refused for a stale confirmation. Iterate until ancestor retention settles.
        changed = True
        while changed:
            changed = False
            for tid, target in targets.items():
                if not reasons[tid] and any(reasons[other] and _preview_overlap(target['identity'], child['identity']) in {'same', 'contains'}
                                            for other, child in targets.items() if other != tid):
                    reasons[tid] = 'contains_retained_target'; changed = True
        history = project.get('cleanup_history', [])
        if not isinstance(history, list):
            raise ProjectManagementError('cleanup_history_unavailable', 'Cleanup history is malformed')
        if custody.lease_live(project.get('cleanup_lease')):
            raise ProjectManagementError('cleanup_in_progress', 'Cleanup is already running', 409)
        mounts = custody.mount_paths()  # unreadable kernel inventory is an explicit failure
        operation = uuid.uuid4().hex
        project['cleanup_lease'] = custody.lease(operation)
        intent = {'id': operation, 'phase': 'started', 'at': utc_now_iso(), 'mode': mode, 'decided_by': 'agent-confirmed' if decided_by == 'agent' else decided_by,
                  'targets': [{'id': tid, 'path': t['path'], 'reconstruction': t['reconstruction'],
                               'warning_token': confirmations.get(tid), 'reason': reasons[tid]} for tid, t in targets.items() if tid in selections]}
        project['cleanup_history'] = [*history, intent]
        registry._save(drive_root, data)  # reconstruction is durable BEFORE deleting its checkout
    results = []
    history_error = ''
    try:
        for tid, target in sorted(targets.items(), key=lambda pair: len(pathlib.Path(pair[1]['path']).parts), reverse=True):
            reason = reasons[tid] or ('history_publication_failed' if history_error else '')
            status = 'refused' if reason and tid in selections else 'retained' if reason else 'removed'
            if not reason:
                removal_started = False
                try:
                    # Re-read lifecycle, sharing, process custody and physical identity
                    # before each effect. The lease fences Reopen and a second cleanup;
                    # the global registry lock is NEVER held during recursive deletion.
                    fresh_identity = _preview_identity(target['path'])
                    fresh_risk, _ = _risk(pathlib.Path(target['path']), fresh_identity)
                    risk_cache[target['path']] = fresh_risk
                    with registry._file_write_lock(registry._registry_path(drive_root)):
                        fresh = registry._load(drive_root, strict=True)
                        row = next(p for p in fresh['projects'] if p['id'] == project_id)
                        if row['lifecycle'] != registry.PROJECT_TOMBSTONED or row.get('cleanup_lease', {}).get('id') != operation:
                            raise ProjectManagementError('cleanup_lease_lost', 'Cleanup ownership or lifecycle changed', 409)
                        current = _plan(drive_root, project_id, mode, system_repo_dir, fresh['projects'], read_authorize, risk_cache)
                        now = next((t for t in current['targets'] if t['id'] == tid), None)
                        if now is None or now['identity'] != target['identity']:
                            status, reason = 'changed_since_confirmation', 'target_identity_changed'
                        elif now['retain_reason']:
                            status, reason = 'refused', now['retain_reason']
                        elif now['exists'] and confirmations.get(tid) != (now['warning']['token'] if now['warning'] else now['selection_token']):
                            status, reason = 'changed_since_confirmation', 'risk_or_selection_changed'
                        elif any(child['id'] != tid and _preview_overlap(now['identity'], child['identity']) in {'same', 'contains'}
                                 and (child['id'] not in selections or child['retain_reason'] or reasons.get(child['id'])
                                      or any(r['id'] == child['id'] and r['status'] not in {'removed', 'already_absent'} for r in results)
                                      or (child['exists'] and confirmations.get(child['id']) != (child['warning']['token'] if child['warning'] else child['selection_token'])))
                                 for child in current['targets']):
                            status, reason = 'refused', 'contains_retained_target'
                        elif custody.contains_mount(now['identity']['resolved_path'], mounts):
                            status, reason = 'refused', 'mount_point_or_contains_mount'
                        elif authorize and authorize(now['path']):
                            status, reason = 'refused', 'physical_access_changed'
                    if not reason:
                        if not now['exists']:
                            status, reason = 'already_absent', 'missing'
                        else:
                            # lstat protects the leaf itself, resolution protects aliases.
                            before = pathlib.Path(now['path']).lstat()
                            if ((before.st_dev, before.st_ino) != tuple(now['operand_inode']) or
                                    _preview_identity(now['path']) != {k: v for k, v in now['identity'].items() if k != 'operand_inode'}):
                                status, reason = 'changed_since_confirmation', 'operand_identity_changed'
                            elif now.get('copy_record'):
                                removal_started = True
                                if not custody.retire_copy(drive_root, now['copy_record']):
                                    status, reason = 'refused', 'retirement_owner_retained_copy'
                            else:
                                removal_started = True
                                _remove(now)
                            if not reason and (pathlib.Path(now['path']).exists() or pathlib.Path(now['path']).is_symlink()):
                                status, reason = 'partially_removed', 'target_survived_removal'
                except Exception as exc:
                    # rmtree may have removed some entries before raising. Never
                    # report an untouched failure (or success) for that outcome.
                    path = pathlib.Path(target['path'])
                    status = 'partially_removed' if removal_started and (path.exists() or path.is_symlink()) else 'failed'
                    reason = f'{type(exc).__name__}: {exc}'
            outcome = {'id': tid, 'path': target['path'], 'status': status, 'reason': reason, 'bytes_removed': None}
            results.append(outcome)
            try:
                with registry._file_write_lock(registry._registry_path(drive_root)):
                    saved = registry._load(drive_root, strict=True)
                    row = next(p for p in saved['projects'] if p['id'] == project_id)
                    row['cleanup_history'].append({'id': operation, 'phase': 'target', 'at': utc_now_iso(), **outcome})
                    registry._save(drive_root, saved)
            except Exception as exc:
                # Stop further effects on lost history, but retain an outcome for
                # every target and attempt the terminal record below. Persistent
                # storage failure still raises; it cannot truthfully be hidden.
                history_error = f'{type(exc).__name__}: {exc}'
                outcome['history_error'] = history_error
        result = {'id': operation, 'phase': 'finished', 'at': utc_now_iso(), 'project_id': project_id,
                  'mode': mode, 'decided_by': 'agent-confirmed' if decided_by == 'agent' else decided_by, 'results': results,
                  **({'error': 'Cleanup history publication failed: ' + history_error} if history_error else {})}
        with registry._file_write_lock(registry._registry_path(drive_root)):
            saved = registry._load(drive_root, strict=True)
            row = next(p for p in saved['projects'] if p['id'] == project_id)
            row['cleanup_history'].append(result)
            registry._save(drive_root, saved)
        return result
    finally:
        with registry._file_write_lock(registry._registry_path(drive_root)):
            saved = registry._load(drive_root, strict=True)
            row = next(p for p in saved['projects'] if p['id'] == project_id)
            if row.get('cleanup_lease', {}).get('id') == operation:
                row.pop('cleanup_lease', None)
                registry._save(drive_root, saved)
