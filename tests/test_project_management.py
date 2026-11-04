"""Management is context and lifecycle, never filesystem setup or new authority."""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import project_management as management
from ouroboros import projects_registry as registry


def close(root, pid):
    registry.begin_project_deletion(root, pid)
    return registry.complete_project_deletion(root, pid)


def snapshot(path):
    return {str(item.relative_to(path)): (item.read_bytes(), item.stat().st_mtime_ns)
            for item in path.rglob('*') if item.is_file()}


def test_legacy_default_detach_and_duplicate_shared_associations(tmp_path):
    root, folder = tmp_path / 'data', tmp_path / 'not-created'
    row = registry.create_project(root, 'alpha', working_dir=str(folder))
    registry.create_project(root, 'beta')
    before = registry.project_admission_view(root, 'alpha', frozen=True)
    assert management.list_project_resources(root, 'alpha')[0]['path'] == str(folder)
    assert management.add_project_resource(root, 'alpha', str(folder))['resources'][0]['id'] == 'default'
    assert 'resources' not in registry.get_reserved_project(root, 'alpha')
    assert management.detach_project_resource(root, 'alpha', 'default')['resources'] == []
    assert management.list_project_resources(root, 'alpha') == []
    shared = str(tmp_path / 'shared')
    first = management.add_project_resource(root, 'alpha', shared, 'library')['resources'][0]
    assert management.add_project_resource(root, 'alpha', shared + '/.')['resources'] == [first]
    second = management.add_project_resource(root, 'beta', shared)['resources'][0]
    assert second['path'] == first['path'] and second['id'] != first['id']
    assert registry.project_admission_view(root, 'alpha', frozen=True) == before
    assert registry.get_project(root, 'alpha')['working_dir'] == row['working_dir']
    assert not folder.exists() and not pathlib.Path(shared).exists()
    # Context association has no whitelist: arbitrary inaccessible paths remain useful context.
    assert management.add_project_resource(root, 'beta', '/outside/unavailable')['resources'][-1]['path'] == '/outside/unavailable'


@pytest.mark.parametrize('legacy', [False, True])
def test_reopen_preserves_identity_data_and_fences_old_frozen_work(tmp_path, legacy):
    root, folder = tmp_path / 'data', tmp_path / 'missing'
    created = registry.create_project(root, 'alpha', name='Alpha', working_dir=str(folder))
    management.add_project_resource(root, 'alpha', str(tmp_path / 'other'), 'Other')
    if legacy:
        data = json.loads(registry._registry_path(root).read_text())
        data['projects'][0].pop('routing_incarnation')
        registry._registry_path(root).write_text(json.dumps(data))
    basis = registry.project_admission_view(root, 'alpha', frozen=True)
    memory = root / 'projects' / 'alpha'
    memory.mkdir(parents=True)
    (memory / 'workpad.md').write_text('unchanged memory')
    (root / 'state' / 'queue_snapshot.json').write_text('{"pending":[]}')
    registry.bind_task_to_project(root, 'old-task', 'alpha', origin={'absent': 'system'})
    closed = close(root, 'alpha')
    data_before = snapshot(memory)
    queue_before = (root / 'state' / 'queue_snapshot.json').read_bytes()
    bindings_before = registry._bindings_path(root).read_bytes()
    reopened = management.reopen_project(root, 'alpha')
    assert reopened['id'] == created['id'] and reopened['chat_id'] == created['chat_id']
    assert reopened['name'] == closed['name'] and reopened['working_dir'] == closed['working_dir']
    assert reopened['resources'] == closed['resources']
    assert reopened['routing_incarnation'] and reopened['routing_incarnation'] != closed.get('routing_incarnation')
    assert reopened['routing_generation'] == closed['routing_generation'] + 1
    assert not folder.exists() and snapshot(memory) == data_before
    assert (root / 'state' / 'queue_snapshot.json').read_bytes() == queue_before
    assert registry._bindings_path(root).read_bytes() == bindings_before
    with pytest.raises(registry.ProjectAdmissionError, match='changed'):
        with registry.project_admission_guard(root, basis):
            pass
    new_basis = registry.project_admission_view(root, 'alpha', frozen=True)
    with registry.project_admission_guard(root, new_basis):
        pass
    unchanged = registry._registry_path(root).read_bytes()
    assert management.reopen_project(root, 'alpha')['routing_incarnation'] == reopened['routing_incarnation']
    assert registry._registry_path(root).read_bytes() == unchanged


def test_closing_reopen_refused_and_resources_unchanged(tmp_path):
    registry.create_project(tmp_path, 'alpha')
    registry.begin_project_deletion(tmp_path, 'alpha')
    before = registry._registry_path(tmp_path).read_bytes()
    for operation in (lambda: management.reopen_project(tmp_path, 'alpha'),
                      lambda: management.add_project_resource(tmp_path, 'alpha', '/somewhere')):
        with pytest.raises(management.ProjectManagementError) as error:
            operation()
        assert error.value.reason == 'project_closing'
    assert registry._registry_path(tmp_path).read_bytes() == before


def test_catalog_pagination_search_and_no_observation(tmp_path, monkeypatch):
    for number in range(7):
        registry.create_project(tmp_path, f'p{number}', name=f'Room {number}', working_dir=f'/missing/{number}')
    close(tmp_path, 'p0')
    registry.begin_project_deletion(tmp_path, 'p1')
    monkeypatch.setattr(management, 'observe_resource', lambda path: pytest.fail('catalog must not probe paths'))
    monkeypatch.setattr(management.subprocess, 'run', lambda *a, **k: pytest.fail('catalog must not run Git'))
    page = management.project_catalog(tmp_path, offset=0, limit=3)
    next_page = management.project_catalog(tmp_path, offset=3, limit=3)
    assert page['total'] == 7 and page['has_more'] and page['offset'] == 0
    assert len(page['projects']) == 3
    assert not {p['id'] for p in page['projects']} & {p['id'] for p in next_page['projects']}
    assert management.project_catalog(tmp_path, offset=6, limit=3)['has_more'] is False
    assert management.project_catalog(tmp_path, offset=70)['projects'] == []
    assert management.project_catalog(tmp_path, lifecycle='active')['total'] == 5
    assert {p['lifecycle_label'] for p in management.project_catalog(tmp_path, lifecycle='closed')['projects']} == {'closed', 'closing'}
    assert management.project_catalog(tmp_path, q='ROOM 4')['projects'][0]['id'] == 'p4'
    assert len(registry.list_sidebar_projects(tmp_path)) == 6  # original sidebar semantics
    for kwargs in ({'limit': 0}, {'offset': -1}, {'lifecycle': 'bogus'}):
        with pytest.raises(management.ProjectManagementError):
            management.project_catalog(tmp_path, **kwargs)


def test_unexpandable_resource_is_invalid_and_retained_path_is_unavailable(tmp_path, monkeypatch):
    registry.create_project(tmp_path, 'alpha')
    before = registry._registry_path(tmp_path).read_bytes()
    def unavailable_home(_path):
        raise RuntimeError('Could not determine home directory')
    monkeypatch.setattr(pathlib.Path, 'expanduser', unavailable_home)
    with pytest.raises(management.ProjectManagementError) as error:
        management.add_project_resource(tmp_path, 'alpha', '~missing-user/context')
    assert error.value.reason == 'invalid_resource'
    assert registry._registry_path(tmp_path).read_bytes() == before
    observed = management.observe_resource('~missing-user/context')
    assert observed['status'] == 'unavailable' and 'RuntimeError' in observed['error']


def test_corrupt_registry_never_becomes_empty_or_mutates(tmp_path):
    registry.create_project(tmp_path, 'alpha')
    registry._registry_path(tmp_path).write_text('{broken')
    before = registry._registry_path(tmp_path).read_bytes()
    for operation in (lambda: management.project_catalog(tmp_path),
                      lambda: management.project_detail(tmp_path, 'alpha'),
                      lambda: management.reopen_project(tmp_path, 'alpha'),
                      lambda: management.add_project_resource(tmp_path, 'alpha', '/somewhere')):
        with pytest.raises(ValueError):
            operation()
    assert registry._registry_path(tmp_path).read_bytes() == before


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_detail_git_uses_host_locale_and_hidden_process_options(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setenv('LC_ALL', 'de_DE.UTF-8')
    monkeypatch.setattr(management, 'subprocess_hidden_kwargs', lambda: {'creationflags': 123})
    def run(argv, **kwargs):
        calls.append(kwargs)
        assert kwargs['env']['LC_ALL'] == kwargs['env']['LANG'] == 'C'
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert kwargs['creationflags'] == 123
        return SimpleNamespace(returncode=0, stdout='fixture', stderr='')
    monkeypatch.setattr(management.subprocess, 'run', run)
    assert management.observe_resource(str(tmp_path))['git']['status'] == 'repository'
    assert len(calls) == 4


def test_project_entry_facade_preserves_fields_and_contract_text():
    from typing import get_type_hints
    from ouroboros.gateway import contracts, client_contracts

    assert contracts.ProjectEntry is client_contracts.ProjectEntry
    assert 'historical facts' in contracts.ProjectEntry.__doc__
    assert 'idempotent repeat create' in contracts.ProjectEntry.__doc__
    assert get_type_hints(contracts.ProjectEntry)['cognition'] is contracts.ProjectCognition
    assert 'resources' in get_type_hints(contracts.ProjectEntry)


def test_detail_git_two_repos_detached_worktree_missing_and_unavailable(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    a, b = tmp_path / 'repo-a', tmp_path / 'repo-b'
    for path, branch in ((a, 'main'), (b, 'feature-x')):
        path.mkdir()
        git(path, 'init', '-q', '-b', branch)
        (path / 'source.txt').write_text('sentinel')
        git(path, 'add', 'source.txt')
        git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'init')
    registry.create_project(root, 'alpha', working_dir=str(a))
    management.add_project_resource(root, 'alpha', str(b), 'second repo')
    before = (snapshot(a), snapshot(b))
    resources = management.project_detail(root, 'alpha')['resources']
    assert [r['observation']['git']['branch'] for r in resources] == ['main', 'feature-x']
    assert before == (snapshot(a), snapshot(b))
    git(b, 'checkout', '--detach', '-q')
    assert management.project_detail(root, 'alpha')['resources'][1]['observation']['git']['branch'] == ''
    worktree = tmp_path / 'linked'
    git(a, 'worktree', 'add', '-q', '-b', 'linked-branch', str(worktree))
    assert management.observe_resource(str(worktree))['git']['branch'] == 'linked-branch'
    assert management.observe_resource(str(tmp_path / 'absent'))['status'] == 'missing'
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert management.observe_resource(str(plain))['git']['status'] == 'not_repository'
    original = pathlib.Path.stat
    def forbidden(path, *args, **kwargs):
        if str(path) == str(plain):
            raise PermissionError('denied')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'stat', forbidden)
    assert management.observe_resource(str(plain))['status'] == 'unavailable'


def test_management_http_contract_and_error_surfaces(tmp_path, monkeypatch):
    from ouroboros.gateway.router import collect_routes
    import ouroboros.gateway.projects as gateway
    from tests.test_client_contract_bindings import assert_bound

    monkeypatch.setattr(gateway, '_broadcast_projects_changed', lambda *args: None)
    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    app.state.repo_dir = pathlib.Path(__file__).parents[1]
    registry.create_project(tmp_path, 'alpha', working_dir=str(tmp_path / 'missing'))
    close(tmp_path, 'alpha')
    with TestClient(app) as client:
        catalog = client.get('/api/projects/catalog?lifecycle=closed&offset=0&limit=2')
        assert catalog.status_code == 200 and assert_bound('GET /api/projects/catalog', catalog) == 'ProjectCatalogResponse'
        assert catalog.json()['total'] == 1
        detail = client.get('/api/projects/alpha/detail')
        assert detail.json()['project']['resources'][0]['observation']['status'] == 'missing'
        assert_bound('GET /api/projects/{project_id}/detail', detail)
        added = client.post('/api/projects/alpha/resources', json={'path': '/unavailable/context', 'label': 'library'})
        assert added.status_code == 200 and assert_bound('POST /api/projects/{project_id}/resources', added)
        rid = added.json()['project']['resources'][-1]['id']
        detached = client.post(f'/api/projects/alpha/resources/{rid}/detach', json={})
        assert detached.status_code == 200 and assert_bound('POST /api/projects/{project_id}/resources/{resource_id}/detach', detached)
        reopened = client.post('/api/projects/alpha/reopen', json={})
        assert reopened.status_code == 200 and reopened.json()['project']['lifecycle'] == 'active'
        assert_bound('POST /api/projects/{project_id}/reopen', reopened)
        assert client.get('/api/projects/catalog?offset=bad').status_code == 400
        assert client.get('/api/projects/missing/detail').status_code == 404
        assert client.post('/api/projects/alpha/resources', json={'path': 42}).status_code == 400
        registry._registry_path(tmp_path).write_text('{broken')
        assert client.get('/api/projects/catalog').status_code == 500
        assert client.get('/api/projects/alpha/detail').status_code == 500


@pytest.mark.parametrize('project_id', ['catalog', 'from-task', 'ordinary'])
def test_detail_namespace_does_not_shadow_valid_project_ids(tmp_path, project_id):
    from ouroboros.gateway.router import collect_routes
    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    app.state.repo_dir = pathlib.Path(__file__).parents[1]
    registry.create_project(tmp_path, project_id)
    with TestClient(app) as client:
        detail = client.get(f'/api/projects/{project_id}/detail')
        assert detail.status_code == 200
        assert detail.json()['project']['id'] == project_id
        assert client.get('/api/projects/catalog').json()['projects'][0]['id'] == project_id
