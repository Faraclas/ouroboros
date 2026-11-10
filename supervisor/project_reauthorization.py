"""Explicit NEW Project authority for one retained working continuation.

This records a decision on the existing task, not historical admission evidence
and not a Resume grant. Ordinary Project guards and Resume still own dispatch,
controls, money and effect custody. The saved source and old basis stay intact.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from ouroboros.project_admission import (
    project_admission_guard, project_admission_view,
)
from ouroboros.task_results import load_task_result
from ouroboros.utils import utc_now_iso


def recovery_binding(task: dict) -> dict:
    """The particular saved attempt; no model text, clocks or grants are rewritten."""
    return copy.deepcopy({key: task.get(key) for key in (
        'id', '_attempt', 'project_id', 'chat_id', 'workspace_root', '_working_recovery',
    )})


def authorized_basis(task: dict, stored: dict) -> dict | None:
    """A durable explicit decision applies only to its exact held continuation."""
    record = stored.get('project_reauthorization')
    if (isinstance(record, dict) and record.get('binding') == recovery_binding(task)
            and record.get('previous_basis') == task.get('_project_admission')
            and isinstance(record.get('basis'), dict)
            and record.get('owner_source') and record.get('request_id')):
        return copy.deepcopy(record['basis'])
    return None


def _pending(q, task_id):
    rows = [task for task in q.PENDING if task.get('id') == task_id]
    if len(rows) != 1 or task_id in q.RUNNING or task_id in q.ADMISSION_RESERVATIONS:
        raise ValueError('task_not_uniquely_held')
    task = rows[0]
    basis = task.get('_project_admission') or {}
    if (not task.get('_project_admission_restore_hold') or task.get('parent_task_id')
            or not basis.get('legacy_basis') or not task.get('_working_recovery')
            or task.get('_owner_wait_resume') or task.get('_budget_pause_resume')):
        raise ValueError('saved_project_continuation_required')
    return task


def _controls(q, task):
    from ouroboros.cancel_intents import has_active_intent
    from ouroboros.installation_stop import installation_panic
    from ouroboros.owner_pause import fence_closed, read_fence
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, STATUS_CANCEL_REQUESTED

    tid = task['id']
    if installation_panic(q.DRIVE_ROOT, strict=True):
        raise ValueError('panic_held')
    if has_active_intent(q.DRIVE_ROOT, tid, strict=True):
        raise ValueError('cancel_pending')
    if fence_closed(read_fence(q.DRIVE_ROOT, tid)):
        raise ValueError('owner_pause_held')
    stored = load_task_result(q.DRIVE_ROOT, tid, strict=True) or {}
    if not stored or stored.get('status') in _TRULY_TERMINAL_STATUSES | {STATUS_CANCEL_REQUESTED}:
        raise ValueError('task_terminal_or_missing')
    return stored


def authorize_project_recovery(task_id: str, *, request_id: str, issuer_task_id: str,
                               owner_source: dict, checkpoint_sha256: str,
                               project_basis: dict) -> dict:
    """Record new current-Project authorization; never releases an independent hold.

    Capture/check outside Q, serialize with the target launch owner, then compare
    current queue identity and durable task state before writing. A crash before
    queue publication is harmless: the normal hold reader finds this decision.
    Replays return the original decision, not a second write or a new checkpoint.
    """
    from supervisor import queue as q
    from supervisor.continuation_admission import conflicting_writers
    from ouroboros.owner_source import answered_quiz_block, resolve_owner_source
    from ouroboros.owner_pause import launch_lock
    from ouroboros.task_store import patch_document
    from ouroboros.working_checkpoint import recovery_source_for_task

    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        return {'ok': False, 'error': 'request_id_required'}
    try:
        # The authenticated owner door authorizes the operation. The issuer is
        # only a source locator: require its retained origin or own answered quiz.
        # Membership is not consent meaning; the caller interprets the exact words
        # for this target. No keyword matcher can turn a generic plan into consent.
        issuer = load_task_result(q.DRIVE_ROOT, issuer_task_id, strict=True) or {}
        origin = issuer.get('origin_message_ref') or {}
        quiz_source = (owner_source.get('kind') == 'quiz'
                       and owner_source.get('task_id') == issuer_task_id)
        if not quiz_source and (not origin or owner_source != {'kind': 'chat', 'ref': origin}):
            raise ValueError('owner_source_missing')
        source = resolve_owner_source(SimpleNamespace(
            task_id=issuer_task_id, current_chat_id=issuer.get('chat_id'),
            drive_root=q.DRIVE_ROOT, budget_drive_root=q.DRIVE_ROOT), owner_source)
        if not source:
            raise ValueError('owner_source_missing')
        stored = load_task_result(q.DRIVE_ROOT, task_id, strict=True) or {}
        previous = stored.get('project_reauthorization')
        if previous:
            if (previous.get('request_id') != request_id
                    or previous.get('issuer_task_id') != issuer_task_id
                    or previous.get('owner_source') != source
                    or previous.get('basis') != project_basis
                    or (previous.get('binding', {}).get('_working_recovery', {})
                        .get('source_ref', {}).get('sha256')) != checkpoint_sha256):
                raise ValueError('project_authorization_already_recorded')
            return {'ok': True, 'task_id': task_id, 'project_authorization': previous,
                    'resumed': False, 'dispatch_may_proceed': True}
        with q._queue_lock:
            task = copy.deepcopy(_pending(q, task_id))
        binding = recovery_binding(task)
        ref = (task['_working_recovery'].get('source_ref') or {})
        if (not checkpoint_sha256 or ref.get('sha256') != checkpoint_sha256
                or task['_working_recovery'].get('source_task_id') != task_id
                or not recovery_source_for_task(q.DRIVE_ROOT, task)):
            raise ValueError('checkpoint_changed_or_unreadable')
        source_chat = (answered_quiz_block(q.DRIVE_ROOT, issuer_task_id, owner_source.get('quiz_id'))
                       .get('publication_chat_id')
                       if quiz_source else source.get('ref', {}).get('chat_id'))
        if not source or source_chat != task.get('chat_id'):
            raise ValueError('owner_source_missing')
        basis = project_admission_view(q.DRIVE_ROOT, task['project_id'], frozen=True)
        if basis != project_basis or basis['project']['chat_id'] != task.get('chat_id'):
            raise ValueError('project_identity_changed')
        with launch_lock(q.DRIVE_ROOT, task_id):
            _controls(q, task)
            blockers = [row for row in conflicting_writers(q, task_id) if not (
                row.get('kind') == 'dispatchable_member' and row.get('task_id') == task_id)]
            if blockers:
                return {'ok': False, 'error': 'effects_unsettled', 'blockers': blockers}
            with q._queue_lock:
                current = _pending(q, task_id)
                if (recovery_binding(current) != binding
                        or current.get('_project_admission') != task.get('_project_admission')):
                    raise ValueError('saved_continuation_changed')
                # This committed registry read binds the NEW identity, not the old
                # missing incarnation. Close after it is caught again at admission.
                with project_admission_guard(q.DRIVE_ROOT, basis):
                    pass
            record = {'request_id': request_id, 'issuer_task_id': issuer_task_id,
                      'owner_source': source, 'binding': binding, 'basis': basis,
                      'previous_basis': copy.deepcopy(task.get('_project_admission')),
                      'authorized_at': utc_now_iso(), 'kind': 'new_project_authorization'}
            outcome = {}

            def commit(stored):
                if stored.get('project_reauthorization'):
                    previous = stored['project_reauthorization']
                    if (previous.get('request_id') != request_id
                            or any(previous.get(k) != record[k] for k in (
                                'issuer_task_id', 'owner_source', 'binding', 'basis'))):
                        raise ValueError('project_authorization_already_recorded')
                    outcome['record'] = previous
                    return None
                from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, STATUS_CANCEL_REQUESTED

                if not stored or stored.get('status') in _TRULY_TERMINAL_STATUSES | {STATUS_CANCEL_REQUESTED}:
                    raise ValueError('task_terminal_or_missing')
                outcome['record'] = record
                return {**stored, 'project_reauthorization': record}

            patch_document(q.DRIVE_ROOT, task_id, commit)
        return {'ok': True, 'task_id': task_id, 'project_authorization': outcome['record'],
                'resumed': False, 'dispatch_may_proceed': True}
    except (OSError, ValueError, RuntimeError) as exc:
        return {'ok': False, 'error': str(exc)}
