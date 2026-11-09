"""Explicit new Project scope for saved work; history and ordinary controls survive."""
from __future__ import annotations

import copy

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import dialogue_store, projects_registry as projects
from ouroboros.gateway.task_pause import owner_tree_control_routes
from ouroboros.project_dialogue import build_owner_message_ref
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.working_checkpoint import recovery_source_for_task
from supervisor import queue, workers
from tests.test_direct_project_recovery import admit, stopped
from tests.test_project_hold_recovery import worker
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


@pytest.fixture
def saved(host, monkeypatch, tmp_path):
    admitted, project = admit(host, monkeypatch, tmp_path, late=True)
    tid = admitted['task']['id']
    task = stopped(host, admitted)
    task['_project_admission']['legacy_basis'] = True
    task['_project_admission_restore_hold'] = {'reason': 'project_routing_fence_lookup_failed'}
    assert queue.persist_queue_snapshot()
    row = {'direction': 'in', 'chat_id': project['chat_id'], 'client_message_id': 'new-authorization',
           'ts': '2026-11-09T13:46:31+00:00', 'text': 'Authorize the same saved task in this Project'}
    dialogue_store.append(host.root, 'chat', row)
    ref = build_owner_message_ref(chat_id=row['chat_id'], client_message_id=row['client_message_id'],
                                  ts=row['ts'], text=row['text'])
    write_task_result(host.root, 'issuer', 'running', origin_message_ref=ref, chat_id=project['chat_id'])
    body = {'request_id': 'source-bound-decision', 'issuer_task_id': 'issuer',
            'owner_source': {'kind': 'chat', 'ref': ref},
            'checkpoint_sha256': task['_working_recovery']['source_ref']['sha256'],
            'project_basis': projects.project_admission_view(host.root, project['id'], frozen=True)}
    yield task, project, body
    admitted['registry'].unregister(tid)


def post(host, task, body):
    app = Starlette(routes=owner_tree_control_routes())
    app.state.drive_root = host.root
    with TestClient(app) as client:
        return client.post(f"/api/tasks/{task['id']}/project-authorization", json=body)


def test_http_new_authority_keeps_history_and_assigns_same_source_once(host, monkeypatch, saved):
    task, _, body = saved
    tid = task['id']
    state = recovery_source_for_task(host.root, task)
    assert state
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent
    before = copy.deepcopy(task)
    old_record = load_task_result(host.root, tid)
    response = post(host, task, body)
    assert response.status_code == 200, response.text
    record = response.json()['project_authorization']
    assert record['previous_basis'] == before['_project_admission']
    assert record['basis'] == body['project_basis']
    assert record['owner_source']['text'] == 'Authorize the same saved task in this Project'
    assert not response.json()['resumed']
    assert not sent and task == before
    new_record = load_task_result(host.root, tid)
    assert {k: v for k, v in new_record.items() if k != 'project_reauthorization'} == old_record
    workers.assign_tasks()
    workers.assign_tasks()
    assert [row['id'] for row in sent] == [tid]
    assert sent[0]['_attempt'] == 2
    assert sent[0]['_working_recovery'] == before['_working_recovery']
    assert sent[0]['_project_admission'] == body['project_basis']
    assert not host.attempts
    # The operation's retry is a read of the accepted record even after dispatch.
    assert post(host, before, body).json()['project_authorization'] == record
    assert len(sent) == 1
    assert recovery_source_for_task(host.root, before) == state


@pytest.mark.parametrize('bad', ['checkpoint', 'source', 'issuer', 'project', 'extra'])
def test_http_refusals_do_not_change_saved_work(host, saved, bad):
    task, _, body = saved
    original = copy.deepcopy(task)
    stored = load_task_result(host.root, task['id'])
    body = copy.deepcopy(body)
    if bad == 'checkpoint':
        body['checkpoint_sha256'] = '0' * 64
    elif bad == 'source':
        body['owner_source']['ref']['text_sha256'] = '0' * 64
    elif bad == 'issuer':
        body['issuer_task_id'] = 'missing'
    elif bad == 'project':
        body['project_basis']['project']['routing_incarnation'] = 'different'
    else:
        body['grant_all'] = True
    response = post(host, task, body)
    assert response.status_code in {400, 409}, response.text
    assert task == original
    assert load_task_result(host.root, task['id']) == stored


@pytest.mark.parametrize('change', ['attempt', 'source', 'prior_basis'])
def test_decision_is_inert_after_saved_identity_changes(host, monkeypatch, saved, change):
    task, _, body = saved
    assert post(host, task, body).status_code == 200
    if change == 'attempt':
        task['_attempt'] += 1
    elif change == 'source':
        task['_working_recovery']['seq'] += 1
    else:
        task['_project_admission']['project']['created_at'] = 'another-origin'
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent and task['_project_admission_restore_hold']


def test_close_after_authorization_keeps_checkpoint_and_refuses_dispatch(host, monkeypatch, saved):
    task, project, body = saved
    handoff = copy.deepcopy(task['_working_recovery'])
    assert post(host, task, body).status_code == 200
    projects.begin_project_deletion(host.root, project['id'])
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent
    assert task['_working_recovery'] == handoff
    assert recovery_source_for_task(host.root, task)


def test_failed_snapshot_rolls_back_effective_basis(host, monkeypatch, saved):
    task, _, body = saved
    old_basis = copy.deepcopy(task['_project_admission'])
    assert post(host, task, body).status_code == 200
    sent = worker(host, monkeypatch)
    monkeypatch.setattr(queue, 'persist_queue_snapshot', lambda **_: False)
    workers.assign_tasks()
    assert not sent
    assert task['_project_admission'] == old_basis
    assert task['_project_admission_restore_hold']


def test_failed_record_does_not_release_task(host, monkeypatch, saved):
    task, _, body = saved
    def fail(*_args, **_kwargs):
        raise OSError('record unavailable')
    monkeypatch.setattr('ouroboros.task_store.patch_document', fail)
    assert post(host, task, body).status_code == 409
    assert not load_task_result(host.root, task['id']).get('project_reauthorization')
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent


def test_unsettled_effect_cannot_be_authorized(host, monkeypatch, saved):
    task, _, body = saved
    monkeypatch.setattr('supervisor.continuation_admission.conflicting_writers',
                        lambda *_: [{'kind': 'model_handoff', 'task_id': task['id']}])
    response = post(host, task, body)
    assert response.status_code == 409
    assert response.json()['reason_code'] == 'effects_unsettled'
    assert not load_task_result(host.root, task['id']).get('project_reauthorization')


def test_different_request_cannot_replace_record(host, saved):
    task, _, body = saved
    first = post(host, task, body).json()
    assert first['ok']
    assert post(host, task, {**body, 'request_id': 'another-decision'}).status_code == 409
    assert load_task_result(host.root, task['id'])['project_reauthorization'] == first['project_authorization']


@pytest.mark.parametrize('guard', ['owner_hold', 'budget_pause', 'budget_root', 'acceptance', 'reservation'])
def test_scope_decision_does_not_clear_independent_controls(host, monkeypatch, saved, guard):
    task, _, body = saved
    assert post(host, task, body).status_code == 200
    if guard == 'owner_hold':
        task['_owner_hold'] = {'reason': 'owner paused'}
    elif guard == 'budget_pause':
        task['_budget_pause'] = {'status': 'paused_before_dispatch', 'auto_resume': False}
    elif guard == 'budget_root':
        queue.BUDGET_ROOT_FENCES[task['id']] = {'status': 'active'}
    elif guard == 'acceptance':
        queue.ACCEPTANCE_FENCES[task['id']] = {'status': 'sealed'}
    else:
        queue.ADMISSION_RESERVATIONS[task['id']] = 'foreign'
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent and not host.attempts
    if guard == 'owner_hold':
        assert task['_owner_hold'] == {'reason': 'owner paused'}
    elif guard == 'budget_pause':
        assert task['_budget_pause']['auto_resume'] is False


@pytest.mark.parametrize('guard', ['panic', 'cancel', 'running', 'duplicate', 'closed'])
def test_current_controls_refuse_new_authorization(host, monkeypatch, saved, guard):
    task, project, body = saved
    if guard == 'panic':
        monkeypatch.setattr('ouroboros.installation_stop.installation_panic', lambda *a, **k: True)
    elif guard == 'cancel':
        monkeypatch.setattr('ouroboros.cancel_intents.has_active_intent', lambda *a, **k: True)
    elif guard == 'running':
        host.running[task['id']] = {'task': copy.deepcopy(task)}
    elif guard == 'duplicate':
        host.pending.append(copy.deepcopy(task))
    else:
        projects.begin_project_deletion(host.root, project['id'])
    assert post(host, task, body).status_code == 409
    assert not load_task_result(host.root, task['id']).get('project_reauthorization')


def test_restart_after_record_before_queue_release_uses_durable_decision(host, monkeypatch, saved):
    task, _, body = saved
    tid = task['id']
    assert post(host, task, body).status_code == 200
    # The preceding snapshot has only legacy basis; no queue marker was written.
    host.pending.clear()
    queue.restore_pending_from_snapshot()
    [restored] = host.pending
    assert restored['_project_admission']['legacy_basis']
    from tests.test_project_hold_recovery import resume_after_app_stop
    resume_after_app_stop(host, tid)
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert [row['id'] for row in sent] == [tid]
    assert sent[0]['_project_admission'] == body['project_basis']


def test_recovery_closes_unanswered_calls_as_unknown_without_replay(host, saved):
    from ouroboros.working_checkpoint import close_unanswered_calls
    task, _, body = saved
    original = recovery_source_for_task(host.root, task)
    assert post(host, task, body).status_code == 200
    state = recovery_source_for_task(host.root, task)
    assert state == original
    messages = state['messages']
    prior = copy.deepcopy(messages)
    unknown = close_unanswered_calls(messages, 'restart')
    assert unknown == ['call_b']
    assert messages[:len(prior)] == prior
    assert messages[-1]['tool_call_id'] == 'call_b'
    assert 'UNKNOWN' in messages[-1]['content']
    assert 'NOT re-executed' in messages[-1]['content']
    assert not host.attempts
