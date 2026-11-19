"""A terminal task's workspace use is context, not a permanent deletion veto."""
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import projects_registry as r, project_cleanup as cleanup
from ouroboros.gateway.router import collect_routes
from ouroboros.project_management import add_project_resource
from ouroboros.task_results import write_task_result
from tests.test_project_cleanup import setup
from tests.test_repository_risk import repo
from tests.test_project_resource_context_tools import world


@pytest.mark.parametrize('repository', [False, True])
def test_attached_terminal_workspace_requires_selection_and_fresh_warning(setup, tmp_path, repository):
    root, system, _, plan, close, _ = setup
    path = repo(tmp_path) if repository else tmp_path / 'existing-folder'
    path.mkdir(exist_ok=True)
    witness = path / 'unique-local'; witness.write_text('disposable unique work')
    add_project_resource(root, 'alpha', str(path))
    write_task_result(root, 'terminal-work', 'completed', project_id='alpha', workspace_root=str(path))
    close()
    app = Starlette(routes=collect_routes(data_dir=root))
    app.state.drive_root = root; app.state.repo_dir = system
    with TestClient(app) as client:
        response = client.get('/api/projects/alpha/cleanup/plan')
        assert response.status_code == 200, response.text
        target = next(t for t in response.json()['targets'] if t['path'] == str(path))
        assert 'task.workspace_root' in {s.get('source') for s in target['sources']}
        assert target['kind'] == 'attached_external' and not target['default_selected']
        assert not target['retain_reason'] and target['action'] == 'remove_if_selected'
        assert target['warning'] and target['risk']['status'] != 'not_inspected'
        body = {'mode': 'complete', 'selections': [target['id']], 'confirmations': {}}
        refused = client.post('/api/projects/alpha/cleanup', json=body)
        assert refused.json()['results'][0]['reason'] == 'current_warning_confirmation_required'
        assert witness.read_text() == 'disposable unique work'
        body['confirmations'] = {target['id']: target['warning']['token']}
        removed = client.post('/api/projects/alpha/cleanup', json=body)
        assert removed.status_code == 200, removed.text
        assert removed.json()['decided_by'] == 'human'
        assert removed.json()['results'][0]['status'] == 'removed'
        assert not path.exists()


def test_registered_agent_terminal_workspace_loss_confirmation(world):
    data, repos, ctx, tools = world
    path = repos[0]
    write_task_result(data, 'terminal-work', 'completed', project_id='room', workspace_root=str(path))
    r.begin_project_deletion(data, 'room'); r.complete_project_deletion(data, 'room')
    def call(**kwargs):
        result = tools.execute_result('project_cleanup', {'project_id': 'room', **kwargs})
        assert result.status == 'ok', result.text
        return json.loads(result.text)
    target = next(t for t in call()['targets'] if t['path'] == str(path))
    assert not target['retain_reason'] and not target['default_selected'] and target['warning']
    args = {'action': 'execute', 'selections': [target['id']], 'paths': [str(path)]}
    refused = call(**args, confirmations={})
    assert refused['results'][0]['reason'] == 'current_warning_confirmation_required'
    assert (path / 'input.txt').read_text() == 'source'
    result = call(**args, confirmations={target['id']: target['warning']['token']})
    assert result['decided_by'] == 'agent-confirmed'
    assert result['results'][0]['status'] == 'removed'
    assert not path.exists() and (repos[1] / 'input.txt').read_text() == 'other'


@pytest.mark.parametrize('guard', ['runtime', 'memory', 'shared', 'live', 'artifact', 'habitat'])
def test_terminal_workspace_does_not_override_real_protection(setup, tmp_path, guard):
    root, system, _, plan, close, execute = setup
    path = system if guard == 'runtime' else root / 'memory' if guard == 'memory' else tmp_path / 'external'
    path.mkdir(parents=True, exist_ok=True)
    witness = path / 'keep'; witness.write_text('must stay')
    fields = {'workspace_root': str(path)}
    if guard == 'artifact': fields['artifacts'] = [{'path': str(path)}]
    write_task_result(root, 'terminal-work', 'completed', project_id='alpha', **fields)
    if guard == 'shared': r.create_project(root, 'beta', working_dir=str(path))
    if guard == 'habitat': r.create_project(root, 'viper-discord', working_dir=str(path))
    if guard == 'live': write_task_result(root, 'live-work', 'running', project_id='beta', workspace_root=str(path))
    close(); target = plan()['targets'][0]
    expected = {'runtime': 'runtime_or_habitat_overlap', 'memory': 'runtime_or_habitat_overlap',
                'shared': 'shared_with_another_project', 'live': 'live_task_or_process_uses_target',
                'artifact': 'runtime_memory_or_recorded_artifact', 'habitat': 'runtime_or_habitat_overlap'}[guard]
    assert target['retain_reason'] == expected
    outcome = execute([target['id']], {target['id']: target['selection_token']})
    assert outcome['results'][0]['status'] == 'refused' and witness.read_text() == 'must stay'


def test_nested_terminal_workspace_needs_its_own_selection(setup, tmp_path):
    root, system, _, plan, close, execute = setup
    parent = tmp_path / 'attached'; parent.mkdir()
    child = parent / 'nested'; child.mkdir(); (child / 'keep').write_text('unique')
    add_project_resource(root, 'alpha', str(parent))
    write_task_result(root, 'terminal-work', 'completed', project_id='alpha', workspace_root=str(child))
    close(); targets = {t['path']: t for t in plan()['targets']}
    outer, inner = targets[str(parent)], targets[str(child)]
    assert inner['kind'] == 'task_associated' and not inner['default_selected'] and inner['warning']
    assert not inner['retain_reason']
    result = execute([outer['id']], {outer['id']: outer['warning']['token']})
    assert result['results'][0]['reason'] == 'contains_retained_target'
    assert (child / 'keep').read_text() == 'unique'
    tokens = {t['id']: t['warning']['token'] for t in plan()['targets']}
    result = execute(list(tokens), tokens)
    assert [row['path'] for row in result['results']] == [str(child), str(parent)]
    assert all(row['status'] == 'removed' for row in result['results']) and not parent.exists()
