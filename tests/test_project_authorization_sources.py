"""Recovery source membership uses canonical answered decisions, not text heuristics."""
import copy

import pytest

from ouroboros import owner_quiz
from ouroboros.task_results import load_task_result
from tests.test_project_reauthorization import saved, post  # noqa: F401
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


@pytest.mark.parametrize('answered', [False, True])
def test_exact_issuer_quiz_source_requires_a_recorded_answer(host, saved, answered):
    task, project, body = saved
    owner_quiz.record_asked(host.root, 'issuer', quiz_id='scope',
        question='Authorize this saved continuation in the current Project?',
        options=['Authorize', 'Keep held'], chat_id=project['chat_id'])
    owner_quiz.record_publication(host.root, 'issuer', 'scope', project['chat_id'])
    if answered:
        assert owner_quiz.record_answered(host.root, 'issuer', quiz_id='scope',
            option_index=0, request_id='answer')['ok']
    body = {**body, 'owner_source': {'kind': 'quiz', 'task_id': 'issuer', 'quiz_id': 'scope'}}
    before = copy.deepcopy(task)
    response = post(host, task, body)
    assert response.status_code == (200 if answered else 409), response.text
    assert task == before
    if answered:
        result = response.json()
        assert result['resumed'] is False and result['dispatch_may_proceed'] is True
        source = result['project_authorization']['owner_source']
        assert source['kind'] == 'quiz' and source['quiz_id'] == 'scope'
        assert 'Authorize this saved continuation' in source['question_frame']
    else:
        assert not load_task_result(host.root, task['id']).get('project_reauthorization')


def test_effect_custody_is_rechecked_under_launch_lock(host, monkeypatch, saved):
    from contextlib import contextmanager
    task, _, body = saved
    entered = False
    @contextmanager
    def launch(*args):
        nonlocal entered
        entered = True
        yield
    def blockers(*args):
        assert entered, 'effect custody was observed before serialization with target launch'
        return [{'kind': 'model_handoff', 'task_id': task['id']}]
    monkeypatch.setattr('ouroboros.owner_pause.launch_lock', launch)
    monkeypatch.setattr('supervisor.continuation_admission.conflicting_writers', blockers)
    response = post(host, task, body)
    assert response.status_code == 409 and response.json()['reason_code'] == 'effects_unsettled'
