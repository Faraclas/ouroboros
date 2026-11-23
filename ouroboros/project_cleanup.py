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
INTERNAL_TASK_SOURCES = frozenset({
    'task.artifacts',
    'task.child_drive_root',
    'task.headless_child_drive_root',
    'task.workspace_root',
})


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


def _plan(drive_root, project_id, mode, system_repo_dir, projects, authorize=None, risk_cache=None, *, page=None, selected_ids=None, inspect_bulk=False, omit_internal=False):
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
    # A request observes each physical identity once. Comparisons reuse parsed
    # components too; Path.is_relative_to walks ancestors on Python 3.14.
    components = {}
    def parts(path):
        if path not in components:
            components[path] = pathlib.Path(os.path.normcase(path)).parts
        return components[path]
    def overlap(left, right):
        return _preview_overlap(left, right, components)
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
    drive_root_identity = identity(str(pathlib.Path(drive_root).expanduser().resolve()))
    drive_root_parts = parts(drive_root_identity['resolved_path'])
    def is_internal_runtime(item):
        if item['access_denial']:
            return False
        sources_set = {s.get('source') for s in item['sources']}
        return (bool(sources_set) and sources_set <= INTERNAL_TASK_SOURCES
                and parts(item['path'])[:len(drive_root_parts)] == drive_root_parts
                and overlap(item['identity'], drive_root_identity) in {'same', 'inside'})
    copy_identities = [(copy, identity(copy['path'])) for copy in facts_context['copies']]
    workspace_identities = [identity(p) for p in facts_context['workspaces']]
    cwd_parts = [parts(cwd) for cwd in facts_context['process_cwds']]
    omitted_internal_total = 0
    internal_target_ids = set()
    active_items = []
    for item in grouped.values():
        internal = is_internal_runtime(item)
        if internal:
            omitted_internal_total += 1
            internal_target_ids.add('cleanup:' + _hash(item['path']))
        if not (omit_internal and internal):
            active_items.append(item)
    inspected_paths = None
    if page is not None:
        offset, limit, query = page
        matches = [item['path'] for item in active_items if query.casefold() in item['path'].casefold()]
        inspected_paths = set(matches[offset:offset + limit])
    targets = []
    for item in active_items:
        path, observed, sources = pathlib.Path(item['path']), item['identity'], item['sources']
        origins = {s.get('origin', 'unknown') for s in sources}
        sources_set = {s.get('source') for s in sources}
        created = any(s.get('source') in {'genesis_producer', 'clone_producer', 'create_producer'}
                      and s.get('origin') in {'project_created', 'genesis', 'cloned'} for s in sources)
        temp = 'private_copy_record' in sources_set
        output = 'recorded_output' in origins
        kind = ('recorded_output' if output else 'project_created' if created else 'generated_temp' if temp
                else 'task_associated' if sources_set == {'task.workspace_root'} else 'attached_external')
        protection = 'runtime_or_habitat_overlap' if any(overlap(observed, p) for p in protected) else ''
        for fixed, inside in fixed_identities:
            relation = overlap(observed, fixed)
            if relation in ({'same', 'contains', 'inside'} if inside else {'same', 'contains'}):
                protection = 'runtime_or_habitat_overlap'
        if sources_set & {'task.child_drive_root', 'task.headless_child_drive_root', 'task.artifacts'}:
            protection = 'runtime_memory_or_recorded_artifact'
        # A terminal task's workspace is provenance, not a retention policy.
        # Existing external targets remain opt-in with current loss confirmation;
        # runtime, live/shared use and Git dependencies are checked independently.
        copy_record = None
        if not protection:
            for copy, copy_identity in copy_identities:
                relation = overlap(observed, copy_identity)
                owner = copy.get('task_id')
                owned = owner in facts_context['task_ids'] or owner == f'project_{project_id}'
                if relation == 'same' and owned:
                    copy_record = copy
                elif relation:
                    protection = 'another_copy_or_copy_ancestor'
        if facts_context['processes'] or any(overlap(observed, p) for p in workspace_identities):
            protection = 'live_task_or_process_uses_target'
        if not protection and any(overlap(observed, p) for p in others):
            protection = 'shared_with_another_project'
        if any(cwd[:len(parts(observed['resolved_path']))] == parts(observed['resolved_path']) for cwd in cwd_parts):
            protection = protection or 'process_cwd_inside'
        target_id = 'cleanup:' + _hash(item['path'])
        if item['access_denial']:
            protection = 'caller_access_denied'
        exists = None if item['access_denial'] else path.exists() or path.is_symlink()
        bulk_candidate = mode == 'complete' and exists is True and (created or temp) and not output and not protection
        # The complete browser preview inspects the prospective bulk set, not every
        # recorded target. A page may omit its rows, never its eligibility facts.
        deferred = ((inspected_paths is not None and item['path'] not in inspected_paths
                     and target_id not in (selected_ids or ()) and not (inspect_bulk and bulk_candidate))
                    or (inspected_paths is None and selected_ids is not None and target_id not in selected_ids))
        cached = None if risk_cache is None and not deferred else (risk_cache or {}).get(item['path'], {
            'status': 'unavailable', 'gaps': ['New target requires inspection outside the registry lock'],
            'facts': {}, 'cleanup_retain_reason': 'new_target_requires_inspection'})
        # Loss inspection cannot make an already-protected target removable.
        # Report it as uninspected, and inspect afresh if that protection ends.
        if protection:
            risk = {'status': 'not_inspected', 'facts': {},
                    'gaps': ['Loss inspection not performed: ' + protection],
                    'cleanup_retain_reason': protection}
            warning = None
        elif exists is False:
            risk, warning = {'status': 'missing', 'facts': {}, 'gaps': []}, None
        else:
            risk, warning = _risk(path, observed, cached)
        if not warning and not (created or temp) and risk['status'] not in {'missing'}:
            warning = {'message': 'This existing or unknown-origin target may contain unique local material. Association is not disposability. Explicitly confirm its loss.',
                       'token': _hash([str(path), observed, risk]), 'unique_work': False, 'unknown': True}
        if any(overlap(observed, p) for p in closed_others):
            warning = {'message': 'Another closed Project still references this path. Explicitly confirm removing its local material.',
                       'token': _hash([str(path), observed, risk, 'closed_shared']), 'unique_work': True, 'unknown': True}
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
        eligible = bool(bulk_candidate and not retain_reason)
        targets.append({'id': target_id, 'path': item['path'], 'kind': kind,
                        'origins': sorted(origins), 'sources': sources, 'identity': observed,
                        'exists': exists, 'protection': protection, 'retain_reason': retain_reason,
                        'access_denial': item['access_denial'],
                        'risk': risk, 'warning': warning, 'reconstruction': reconstruction, 'copy_record': copy_record,
                        'selection_token': _hash([target_id, observed, risk]), 'operand_inode': observed['operand_inode'],
                        'eligible': eligible, 'inspection_deferred': deferred and not protection and exists is True,
                        'default_selected': bool(exists is True and (created or temp) and not output and not retain_reason and
                                                 (mode == 'complete' or (reconstruction and not warning))),
                        'action': 'already_absent' if exists is False and not retain_reason else 'retain' if retain_reason else 'remove_if_selected'})
    return {'project_id': project_id, 'mode': mode, 'lifecycle': project['lifecycle'],
            'targets': targets, 'gaps': [*generated['gaps'], *(['Some recorded targets are retained without loss inspection because caller access is denied.'] if any(t['access_denial'] for t in targets) else [])],
            'omitted_internal_total': omitted_internal_total,
            '_internal_target_ids': frozenset(internal_target_ids),
            'coverage': 'Recorded targets only. No container inventory or backup verification. Remote refs are local knowledge, not a live push check.'}


def cleanup_plan(drive_root, project_id, mode='complete', system_repo_dir=None, *, authorize=None, selected_ids=None):
    """Read-only explicit inspection. Association alone never selects deletion."""
    plan = _plan(drive_root, project_id, mode, system_repo_dir,
                 registry._load(drive_root, strict=True)['projects'], authorize, selected_ids=selected_ids)
    plan.pop('_internal_target_ids', None)
    return plan


def _target_facts(targets):
    return [[t['id'], t['path'], t['identity'], t['exists'], t['protection'], t['access_denial'], t['sources']]
            for t in targets]


def _bulk_digest(project_id, mode, targets):
    return _hash([project_id, mode, _target_facts(targets),
                  [[t['id'], t['selection_token'], t['warning']] for t in targets if t['eligible']]])


def cleanup_plan_page(drive_root, project_id, mode='complete', system_repo_dir=None, *,
                      authorize=None, offset=0, limit=25, query='', plan_sha256='', inspect_bulk=False):
    """Bounded current observation; never substitute this page for execution's plan.

    Pages are current rereads, not a stored snapshot or deletion lease. A digest
    mismatch refuses navigation rather than mixing selections from different plans.
    """
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or not isinstance(query, str) or len(query) > 2000 or not isinstance(plan_sha256, str)):
        raise ProjectManagementError('invalid_cleanup', 'offset >=0, limit 1..100 and query <=2000 characters are required')
    plan = _plan(drive_root, project_id, mode, system_repo_dir,
                 registry._load(drive_root, strict=True)['projects'], authorize,
                 page=(offset, limit, query), inspect_bulk=inspect_bulk, omit_internal=True)
    plan.pop('_internal_target_ids', None)
    targets = plan['targets']
    bulk = inspect_bulk and mode == 'complete'
    targets_digest = _bulk_digest(project_id, mode, targets) if bulk else ''
    digest = _hash([plan['lifecycle'], targets_digest]) if bulk else _hash([project_id, mode, plan['lifecycle'], _target_facts(targets), plan['omitted_internal_total']])
    if plan_sha256 and plan_sha256 != digest:
        raise ProjectManagementError('cleanup_plan_changed', 'Cleanup facts changed; refresh inspection before continuing', 409)
    matches = [t for t in targets if query.casefold() in t['path'].casefold()]
    page = matches[offset:offset + limit]
    next_offset = offset + len(page)
    protected_total = sum(bool(t['protection']) or (t['exists'] is True and not t['inspection_deferred'] and bool(t['retain_reason'])) for t in targets)
    already_gone_total = sum(not t['protection'] and t['exists'] is False for t in targets)
    eligible_total = sum(bool(t['eligible']) for t in targets)
    uninspected_total = sum(bool(t['inspection_deferred']) for t in targets)
    needs_confirmation_total = sum(t['exists'] is True and not t['retain_reason'] and bool(t['warning']) for t in targets)
    eligible_needs_confirmation_total = sum(bool(t['eligible'] and t['warning']) for t in targets)
    return {**plan, 'targets': page, 'total': len(targets), 'matched_total': len(matches),
            'retained_total': sum(bool(t['protection']) for t in targets),
            'protected_total': protected_total, 'already_gone_total': already_gone_total,
            'eligible_total': eligible_total, 'needs_confirmation_total': needs_confirmation_total,
            'uninspected_total': uninspected_total,
            'eligible_needs_confirmation_total': eligible_needs_confirmation_total,
            'offset': offset, 'limit': limit, 'next_offset': next_offset if next_offset < len(matches) else None,
            'query': query, 'plan_sha256': digest, 'targets_sha256': targets_digest, 'immutable': False,
            'selection_scope': ('Complete cleanup preselects eligible temporary material across all pages. Loss warnings require separate confirmation.' if bulk
                                else 'Only explicitly selected inspected targets are submitted; unseen targets are not selected.')}


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
                    system_repo_dir=None, *, authorize=None, read_authorize=None, pending_request=None,
                    select_all_eligible=False, excluded_ids=(), targets_sha256=''):
    """Replan, confirm current warnings, persist intent, then remove selected paths.

    authorize is the tool caller's existing physical read/write authority check;
    browser requests already originate at the authenticated owner boundary.
    """
    if decided_by not in {'human', 'agent'} or not isinstance(selections, list) or any(not isinstance(s, str) for s in selections):
        raise ProjectManagementError('invalid_cleanup', 'decided_by and a list of target ids are required')
    if not isinstance(confirmations, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in confirmations.items()):
        raise ProjectManagementError('invalid_cleanup', 'confirmations must map target ids to current warning tokens')
    if not isinstance(excluded_ids, (list, tuple)) or any(not isinstance(s, str) for s in excluded_ids):
        raise ProjectManagementError('invalid_cleanup', 'excluded_ids must be a list of target ids')
    if not isinstance(targets_sha256, str):
        raise ProjectManagementError('invalid_cleanup', 'targets_sha256 must be a string')
    effective_selections = set(selections)
    bulk_tokens = {}
    if select_all_eligible:
        if mode != 'complete' or not targets_sha256:
            raise ProjectManagementError('invalid_cleanup', 'Complete mode and targets_sha256 are required with select_all_eligible')
        inspected = _plan(drive_root, project_id, mode, system_repo_dir,
                          registry._load(drive_root, strict=True)['projects'], read_authorize,
                          page=(0, 0, ''), selected_ids=effective_selections, inspect_bulk=True)
        visible_inspected = [t for t in inspected['targets'] if t['id'] not in inspected['_internal_target_ids']]
        if _bulk_digest(project_id, mode, visible_inspected) != targets_sha256:
            raise ProjectManagementError('cleanup_plan_changed', 'Cleanup facts changed; refresh inspection before continuing', 409)
        excluded = set(excluded_ids)
        bulk_tokens = {t['id']: t['selection_token'] for t in inspected['targets'] if t['eligible'] and t['id'] not in excluded}
        effective_selections.update(bulk_tokens)
    else:
        inspected = cleanup_plan(drive_root, project_id, mode, system_repo_dir, authorize=read_authorize, selected_ids=effective_selections)

    def confirmation_reason(target):
        if not target['exists']:
            return ''
        tid = target['id']
        if target['warning']:
            return '' if confirmations.get(tid) == target['warning']['token'] else 'current_warning_confirmation_required'
        return '' if bulk_tokens.get(tid, confirmations.get(tid)) == target['selection_token'] else 'current_selection_confirmation_required'
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
        visible_plan = [t for t in plan['targets'] if t['id'] not in plan['_internal_target_ids']]
        if select_all_eligible and _bulk_digest(project_id, mode, visible_plan) != targets_sha256:
            raise ProjectManagementError('cleanup_plan_changed', 'Cleanup facts changed; refresh inspection before continuing', 409)
        if effective_selections - targets.keys():
            raise ProjectManagementError('cleanup_plan_changed', 'Unknown target id; refresh the cleanup plan', 409)
        reasons = {}
        for tid, target in targets.items():
            reason = target['retain_reason'] or ('not_selected' if tid not in effective_selections else '')
            if not reason:
                reason = confirmation_reason(target)
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
                               'warning_token': confirmations.get(tid), 'reason': reasons[tid]} for tid, t in targets.items() if tid in effective_selections]}
        project['cleanup_history'] = [*history, intent]
        registry._save(drive_root, data)  # reconstruction is durable BEFORE deleting its checkout
    results = []
    history_error = ''
    try:
        for tid, target in sorted(((tid, t) for tid, t in targets.items() if tid in effective_selections),
                                  key=lambda pair: len(pathlib.Path(pair[1]['path']).parts), reverse=True):
            reason = reasons[tid] or ('history_publication_failed' if history_error else '')
            status = 'refused' if reason and tid in effective_selections else 'retained' if reason else 'removed'
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
                        elif confirmation_reason(now):
                            status, reason = 'changed_since_confirmation', 'risk_or_selection_changed'
                        elif any(child['id'] != tid and _preview_overlap(now['identity'], child['identity']) in {'same', 'contains'}
                                 and (child['id'] not in effective_selections or child['retain_reason'] or reasons.get(child['id'])
                                      or any(r['id'] == child['id'] and r['status'] not in {'removed', 'already_absent'} for r in results)
                                      or confirmation_reason(child))
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
                # every selected target and attempt the terminal record below. Persistent
                # storage failure still raises; it cannot truthfully be hidden.
                history_error = f'{type(exc).__name__}: {exc}'
                outcome['history_error'] = history_error
        result = {'id': operation, 'phase': 'finished', 'at': utc_now_iso(), 'project_id': project_id,
                  'mode': mode, 'decided_by': 'agent-confirmed' if decided_by == 'agent' else decided_by, 'results': results,
                  'unselected_retained_count': sum(1 for tid in targets if tid not in effective_selections and tid not in plan['_internal_target_ids']),
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
