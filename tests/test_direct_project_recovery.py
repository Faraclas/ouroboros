"""Direct Project admission must retain the identity a same-ID restart consumes.

Real direct admission, running writer, working checkpoint, restart transaction,
restore and assignment; no model, real worker termination or live owner data.
"""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

from ouroboros.agent import OuroborosAgent
from ouroboros import projects_registry as projects, working_checkpoint as wc
from ouroboros.task_results import load_task_result
from supervisor import queue, workers, worker_chat_lane as lane
from tests._budget_pause_exact_helpers import _loop_ctx
from tests.test_project_hold_recovery import worker
from tests.test_restart_saved_work import _ack
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


def admit(host, monkeypatch, tmp_path, *, late=False, started=True):
    folder = tmp_path / 'project-folder'
    folder.mkdir()
    project = projects.create_project(host.root, 'direct-room', working_dir=str(folder))
    metadata = {'project_id': project['id']}
    if late:
        metadata['late_answer'] = {'task_id': 'asker', 'quiz_id': 'answered'}
        monkeypatch.setattr('ouroboros.owner_quiz.late_answer_model_text', lambda *_: 'Approved repair')
    actor = SimpleNamespace(env=SimpleNamespace(drive_root=host.root),
                            _owner_message_admission_lock=threading.RLock(), _busy=True,
                            _accepting_owner_messages=True)
    admitted = lane._admit_chat_task(actor, project['chat_id'], 'Approved repair', task_metadata=metadata)
    assert admitted is not None, host.notices
    task = admitted['task']
    actor._current_task_id = task['id']
    actor._current_task_metadata = metadata
    actor._current_task_text = task['text']
    actor._current_chat_id = project['chat_id']
    task['_attempt'] = 1
    task['root_task_id'] = task['id']
    if started:
        OuroborosAgent._persist_running_record(actor, task)
    return admitted, project


def stopped(host, admitted):
    """Use the real direct-roster→durable-result restore path, not a queued surrogate."""
    from supervisor.direct_roots import collect_direct_roots
    from supervisor.restart_retention import prepare_restart_returns

    tid = admitted['task']['id']
    _, limit = _loop_ctx(host.root, tid, direct=True)
    wc.save_round(limit, 'post_batch')  # contains an unresolved call; restore must not rerun it
    assert tid in {row['task_id'] for row in collect_direct_roots()['roots']}
    assert prepare_restart_returns(host.root, {}, [], transaction_id='direct-restart',
                                   direct=[admitted['task']]) == {tid}
    _ack(host.root, 'direct-restart')
    assert queue.persist_queue_snapshot()
    from supervisor.direct_roots import take_direct_roots
    host.monkeypatch.setattr(queue, 'PRIOR_DIRECT_ROOTS', take_direct_roots(host.root))
    admitted['registry'].unregister(tid)  # previous process ended; no native handles are replayed
    assert queue.restore_pending_from_snapshot() == 1
    return next(row for row in host.pending if row['id'] == tid)


@pytest.mark.parametrize('late', [False, True], ids=['project-chat', 'late-answer'])
def test_direct_project_identity_survives_same_id_restart(host, tmp_path, monkeypatch, late):
    admitted, project = admit(host, monkeypatch, tmp_path, late=late)
    tid = admitted['task']['id']
    try:
        expected = projects.project_admission_view(host.root, project['id'], frozen=True)
        assert admitted['task'].get('_project_admission') == expected
        assert load_task_result(host.root, tid).get('_project_admission') == expected
        restored = stopped(host, admitted)
        assert restored['_project_admission'] == expected
        assert not restored['_project_admission'].get('legacy_basis')
        assert restored['_working_recovery']['source_task_id'] == tid
        sent = worker(host, monkeypatch)
        workers.assign_tasks()
        workers.assign_tasks()
        assert [row['id'] for row in sent] == [tid], restored
        assert sent[0]['_attempt'] == 2
        assert sent[0]['_project_admission'] == expected
        assert not host.attempts
    finally:
        admitted['registry'].unregister(tid)


@pytest.mark.parametrize('change', ['incarnation', 'close', 'legacy', 'missing'])
def test_direct_project_restart_does_not_recapture_changed_or_unknown_identity(host, tmp_path, monkeypatch, change):
    admitted, project = admit(host, monkeypatch, tmp_path)
    tid = admitted['task']['id']
    try:
        restored = stopped(host, admitted)
        if change == 'incarnation':
            # A different saved incarnation is not today's room, even with matching id/chat/folder.
            restored['_project_admission']['project']['routing_incarnation'] = 'another-incarnation'
        elif change == 'close':
            projects.begin_project_deletion(host.root, project['id'])
            assert projects.get_reserved_project(host.root, project['id'])['lifecycle'] == 'deleting'
        elif change == 'legacy':
            restored['_project_admission']['legacy_basis'] = True
        else:
            restored.pop('_project_admission', None)
        original = copy.deepcopy(restored.get('_project_admission'))
        sent = worker(host, monkeypatch)
        workers.assign_tasks()
        workers.assign_tasks()
        assert not sent and not host.attempts
        assert restored.get('_project_admission') == original
    finally:
        admitted['registry'].unregister(tid)


@pytest.mark.parametrize('bad_chat', [True, False], ids=['wrong-chat', 'closed-before-bind'])
def test_direct_admission_checks_host_identity_at_binding(host, tmp_path, monkeypatch, bad_chat):
    project = projects.create_project(host.root, 'room')
    if not bad_chat:
        real_bind = projects.bind_task_to_project

        def closed(*args, **kwargs):
            projects.begin_project_deletion(host.root, 'room')
            return real_bind(*args, **kwargs)

        monkeypatch.setattr(projects, 'bind_task_to_project', closed)
    admitted = lane._admit_chat_task(SimpleNamespace(), project['chat_id'] + int(bad_chat), 'work',
        task_metadata={'project_id': 'room', '_project_admission': {'fabricated': True}})
    assert admitted is None
    assert not host.pending and not host.attempts
    assert any(row.get('text') for row in host.notices)


def test_direct_basis_reaches_deferred_queue_and_pause_carrier(host, tmp_path, monkeypatch):
    from ouroboros.budget_pause import parkable_direct_task

    admitted, _ = admit(host, monkeypatch, tmp_path, started=False)
    task = admitted['task']
    admitted['registry'].unregister(task['id'])  # deferred admission has no live actor
    try:
        basis = copy.deepcopy(task['_project_admission'])
        lane._defer_chat_for_restart(task, task['chat_id'], task['metadata'])
        [queued] = host.pending
        assert queued['_project_admission'] == basis
        assert not queued['_project_admission'].get('legacy_basis')
        parked = parkable_direct_task(task)
        assert parked['_project_admission'] == basis
    finally:
        admitted['registry'].unregister(task['id'])


def test_location_update_keeps_restored_direct_target(host, tmp_path, monkeypatch):
    from ouroboros.project_management import update_project_location, project_location
    from ouroboros.workspace_admission import room_chat_lens_dir
    from ouroboros.tools.registry import ToolContext
    admitted, project = admit(host, monkeypatch, tmp_path)
    tid = admitted['task']['id']; old = project['working_dir']
    replacement = tmp_path / 'replacement'; replacement.mkdir()
    try:
        restored = stopped(host, admitted)
        update_project_location(host.root, project['id'], working_dir=str(replacement),
            expected_location=project_location(project), system_repo_dir=workers.REPO_DIR)
        sent = worker(host, monkeypatch); workers.assign_tasks()
        assert len(sent) == 1 and sent[0]['id'] == tid
        directory, note = room_chat_lens_dir(host.root, project['id'], project_admission=sent[0]['_project_admission'])
        ctx = ToolContext(repo_dir=workers.REPO_DIR, drive_root=host.root, task_id=tid,
            project_id=project['id'], is_direct_chat=True, task_metadata={'_project_room_dir': directory, '_project_room_note': note})
        assert str(ctx.active_repo_dir()) == old
        assert room_chat_lens_dir(host.root, project['id'])[0] == str(replacement)
        assert restored['_working_recovery']['source_task_id'] == tid
    finally:
        admitted['registry'].unregister(tid)
