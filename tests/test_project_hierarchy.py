"""Logical project edges share registry transactions, never execution authority."""
from concurrent.futures import ThreadPoolExecutor
import json
import pathlib
import threading

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros import project_management as management, projects_registry as registry


def close(root, pid):
    registry.begin_project_deletion(root, pid)
    return registry.complete_project_deletion(root, pid)


def rows(root):
    return {row['id']: row for row in registry.list_reserved_projects(root, strict=True)}


def test_legacy_read_and_create_repeat_never_migrate_or_reparent(tmp_path):
    registry.create_project(tmp_path, 'parent')
    registry.create_project(tmp_path, 'child', parent_id='parent')
    path = registry._registry_path(tmp_path)
    data = json.loads(path.read_text())
    data['projects'][0].pop('parent_id')
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    assert management.project_detail(tmp_path, 'parent')['parent_id'] == ''
    assert registry.projects_summary(tmp_path)[0]['parent_id'] in {'', 'parent'}
    repeated = registry.create_project(tmp_path, 'child', parent_id='')
    assert repeated['created'] is False and repeated['parent_id'] == 'parent'
    assert path.read_bytes() == before


def test_reparent_moves_only_metadata_and_preserves_task_room(tmp_path):
    from ouroboros.composer_cognition import read_cognition
    for pid in ('parent', 'other', 'child'):
        registry.create_project(tmp_path, pid, working_dir=f'/missing/{pid}')
    registry.bind_task_to_project(tmp_path, 'child-task', 'child', origin={'absent': 'system'})
    management.add_project_resource(tmp_path, 'child', '/other/context', 'Context')
    before = rows(tmp_path)
    basis = registry.project_admission_view(tmp_path, 'child')
    cognition = read_cognition(tmp_path, 'child')
    bindings = registry._bindings_path(tmp_path).read_bytes()
    for parent in ('parent', 'other', ''):
        saved = management.reparent_project(tmp_path, 'CHILD', parent)
        assert saved['id'] == 'child' and saved['parent_id'] == parent
        assert registry.project_admission_view(tmp_path, 'child') == basis
        assert read_cognition(tmp_path, 'child') == cognition
        assert registry.project_chat_for_task(tmp_path, 'child-task') == before['child']['chat_id']
        assert registry._bindings_path(tmp_path).read_bytes() == bindings
        after = rows(tmp_path)
        assert {k: v for k, v in after['child'].items() if k != 'parent_id'} == {
            k: v for k, v in before['child'].items() if k != 'parent_id'}
        assert after['parent'] == before['parent'] and after['other'] == before['other']


def test_invalid_source_never_aliases_an_existing_room(tmp_path):
    registry.create_project(tmp_path, 'child')
    before = registry._registry_path(tmp_path).read_bytes()
    for source in ('child!', ' child', '..-child', [], None):
        with pytest.raises(management.ProjectManagementError):
            management.reparent_project(tmp_path, source, '')
    assert registry._registry_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('bad', [None, False, 2, [], {}, 'Parent', ' parent', '../parent', 'unknown'])
def test_invalid_parent_refuses_without_save(tmp_path, bad):
    registry.create_project(tmp_path, 'parent')
    registry.create_project(tmp_path, 'child')
    before = registry._registry_path(tmp_path).read_bytes()
    with pytest.raises(management.ProjectManagementError):
        management.reparent_project(tmp_path, 'child', bad)
    with pytest.raises(management.ProjectManagementError):
        registry.create_project(tmp_path, 'new-child', parent_id=bad)
    assert registry._registry_path(tmp_path).read_bytes() == before


def test_cycles_and_arbitrary_depth(tmp_path):
    template = registry.create_project(tmp_path, 'p0')
    # Deeper than Python's usual recursion ceiling: supported trees are iterative.
    data = {'projects': [{**template, 'id': f'p{i}', 'chat_id': 100000 + i,
                           'parent_id': f'p{i-1}' if i else ''} for i in range(1100)]}
    registry._save(tmp_path, data)
    saved = registry.create_project(tmp_path, 'leaf', parent_id='p1099')
    assert saved['parent_id'] == 'p1099'
    assert len(management.project_detail(tmp_path, 'leaf')['parent_path']) == 1100
    before = registry._registry_path(tmp_path).read_bytes()
    for pid, parent in [('p0', 'p0'), ('p0', 'leaf'), ('p500', 'p1099')]:
        with pytest.raises(management.ProjectManagementError) as error:
            management.reparent_project(tmp_path, pid, parent)
        assert error.value.reason == 'project_cycle'
    assert registry._registry_path(tmp_path).read_bytes() == before


def test_parent_must_be_active_but_closed_source_may_move(tmp_path):
    for pid in ('active', 'closing', 'closed', 'child'):
        registry.create_project(tmp_path, pid)
    registry.begin_project_deletion(tmp_path, 'closing')
    close(tmp_path, 'closed')
    for parent in ('closing', 'closed'):
        with pytest.raises(management.ProjectManagementError) as error:
            management.reparent_project(tmp_path, 'child', parent)
        assert error.value.reason == 'project_parent_inactive'
        with pytest.raises(management.ProjectManagementError):
            registry.create_project(tmp_path, 'new', parent_id=parent)
    assert management.reparent_project(tmp_path, 'closed', 'active')['parent_id'] == 'active'
    with pytest.raises(management.ProjectManagementError) as error:
        management.reparent_project(tmp_path, 'closing', '')
    assert error.value.reason == 'project_closing'


def test_close_promotes_all_immediate_children_once_and_reopen_reclaims_nothing(tmp_path):
    registry.create_project(tmp_path, 'grand')
    registry.create_project(tmp_path, 'parent', parent_id='grand')
    for pid in ('live', 'closed', 'closing'):
        registry.create_project(tmp_path, pid, parent_id='parent')
    registry.create_project(tmp_path, 'leaf', parent_id='live')
    close(tmp_path, 'closed')
    registry.begin_project_deletion(tmp_path, 'closing')
    registry.bind_task_to_project(tmp_path, 'live-work', 'live', origin={'absent': 'system'})
    basis = registry.project_admission_view(tmp_path, 'live', frozen=True)
    bindings = registry._bindings_path(tmp_path).read_bytes()
    before = rows(tmp_path)
    fenced = registry.begin_project_deletion(tmp_path, 'parent')
    assert fenced['lifecycle'] == 'deleting' and fenced['parent_id'] == 'grand'
    after = rows(tmp_path)
    for pid in ('live', 'closed', 'closing'):
        assert after[pid] == {**before[pid], 'parent_id': 'grand'}
    assert after['leaf'] == before['leaf']
    assert registry.project_admission_view(tmp_path, 'live', frozen=True) == basis
    assert registry._bindings_path(tmp_path).read_bytes() == bindings
    # Failed Close remains fenced; independent child edits survive recovery/retry.
    registry.fail_project_deletion(tmp_path, 'parent', 'pending quiescence')
    management.reparent_project(tmp_path, 'live', '')
    registry.begin_project_deletion(tmp_path, 'parent')
    registry.complete_project_deletion(tmp_path, 'parent')
    management.reopen_project(tmp_path, 'parent')
    assert rows(tmp_path)['live']['parent_id'] == ''
    assert rows(tmp_path)['closed']['lifecycle'] == 'tombstoned'
    assert rows(tmp_path)['closing']['lifecycle'] == 'deleting'
    assert rows(tmp_path)['leaf']['parent_id'] == 'live'
    # Closing its parent also promotes the closed room itself; reopening keeps that edge.
    close(tmp_path, 'parent')
    close(tmp_path, 'grand')
    assert management.reopen_project(tmp_path, 'parent')['parent_id'] == ''


def test_root_close_promotes_children_to_root_and_keeps_grandchildren(tmp_path):
    registry.create_project(tmp_path, 'root')
    registry.create_project(tmp_path, 'child', parent_id='root')
    registry.create_project(tmp_path, 'leaf', parent_id='child')
    registry.begin_project_deletion(tmp_path, 'root')
    assert rows(tmp_path)['child']['parent_id'] == ''
    assert rows(tmp_path)['leaf']['parent_id'] == 'child'


def test_unrelated_writers_preserve_relationship_and_unknown_neighbours(tmp_path):
    from ouroboros.composer_cognition import ensure_project_cognition, update_project_cognition
    registry.create_project(tmp_path, 'parent')
    registry.create_project(tmp_path, 'child', parent_id='parent')
    registry.create_project(tmp_path, 'sibling', parent_id='parent')
    path = registry._registry_path(tmp_path)
    data = json.loads(path.read_text())
    for row in data['projects']:
        row['future_metadata'] = {'retained': row['id']}
    data['projects'][1].pop('cognition')
    path.write_text(json.dumps(data))
    ensure_project_cognition(tmp_path)
    registry.update_project(tmp_path, 'child', name='Renamed', pinned=True,
                            provenance='attached', clone_url='historical', trusted_at='then')
    registry.touch_project(tmp_path, 'child')
    update_project_cognition(tmp_path, 'child', context_mode='max')
    management.update_project_content(tmp_path, 'child', expected_content={'description': '', 'notes': ''}, notes='Words')
    resource = management.add_project_resource(tmp_path, 'child', '/missing/context')['resources'][0]
    management.detach_project_resource(tmp_path, 'child', resource['id'])
    (tmp_path / 'projects' / 'recovered').mkdir(parents=True)
    assert registry.reconcile_projects(tmp_path) == 1
    assert rows(tmp_path)['child']['parent_id'] == 'parent'
    assert rows(tmp_path)['sibling']['parent_id'] == 'parent'
    assert rows(tmp_path)['recovered']['parent_id'] == ''
    for pid in ('parent', 'child', 'sibling'):
        assert rows(tmp_path)[pid]['future_metadata'] == {'retained': pid}


def test_summary_cap_adds_ancestors_without_replacing_recent_or_pin_slice(tmp_path):
    seed = registry.create_project(tmp_path, 'seed', origin='owner_ui')
    data = {'projects': [{**seed, 'id': f'p{i}', 'chat_id': 10000 + i,
                           'last_active_at': f'{i:04}', 'parent_id': ''} for i in range(205)]}
    data['projects'][-1].update(parent_id='p0', pinned=True)
    data['projects'][0]['parent_id'] = 'p1'
    registry._save(tmp_path, data)
    for limit in (50, 200):
        result = registry.projects_summary(tmp_path, limit=limit)
        assert len(result) == limit + 2
        assert [row['id'] for row in result[:limit]] == [f'p{i}' for i in reversed(range(205-limit, 205))]
        assert {row['id'] for row in result} >= {'p204', 'p0', 'p1'}
        assert next(row for row in result if row['id'] == 'p204')['parent_id'] == 'p0'
    pinned = registry.projects_summary(tmp_path, include_pinned=True)
    assert pinned[0]['id'] == 'p204' and len(pinned) == 53


def test_catalog_parent_scope_and_paths_survive_paging_search_and_rename(tmp_path):
    registry.create_project(tmp_path, 'grand', name='Grand')
    registry.create_project(tmp_path, 'parent', name='Parent', parent_id='grand')
    registry.create_project(tmp_path, 'child', name='Needle', parent_id='parent')
    registry.create_project(tmp_path, 'other')
    assert management.project_catalog(tmp_path, parent_id='')['total'] == 2
    page = management.project_catalog(tmp_path, q='needle', parent_id='parent', limit=1)
    assert page['total'] == 1 and len(page['projects']) == 1
    assert page['projects'][0]['parent_path'] == [{'id': 'grand', 'name': 'Grand'}, {'id': 'parent', 'name': 'Parent'}]
    assert page['projects'][0]['parent_path_complete'] is True
    registry.update_project(tmp_path, 'parent', name='Renamed')
    assert management.project_detail(tmp_path, 'child')['parent_path'][-1]['name'] == 'Renamed'
    close(tmp_path, 'child')
    assert management.project_catalog(tmp_path, parent_id='parent', lifecycle='closed')['total'] == 1
    assert management.project_catalog(tmp_path, parent_id='parent', lifecycle='active')['total'] == 0


@pytest.mark.parametrize('parents', [('missing', ''), ('child', ''), ('parent', 'child')])
def test_unsupported_legacy_edges_are_gaps_not_repairs_or_admission_gates(tmp_path, parents):
    registry.create_project(tmp_path, 'parent')
    registry.create_project(tmp_path, 'child')
    data = json.loads(registry._registry_path(tmp_path).read_text())
    data['projects'][1]['parent_id'], data['projects'][0]['parent_id'] = parents
    registry._save(tmp_path, data)
    before = registry._registry_path(tmp_path).read_bytes()
    assert management.project_detail(tmp_path, 'child')['parent_path_complete'] is False
    assert registry.project_admission_view(tmp_path, 'child')['project']['id'] == 'child'
    assert registry._registry_path(tmp_path).read_bytes() == before


def test_concurrent_cycle_and_close_reparent_transactions(tmp_path):
    for pid in ('a', 'b', 'child', 'other'):
        registry.create_project(tmp_path, pid)
    barrier = threading.Barrier(2)
    def move(pair):
        barrier.wait()
        try:
            management.reparent_project(tmp_path, *pair)
            return 'saved'
        except management.ProjectManagementError as exc:
            return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(move, [('a', 'b'), ('b', 'a')])) == ['project_cycle', 'saved']
    management.reparent_project(tmp_path, 'a', '')
    management.reparent_project(tmp_path, 'b', '')
    management.reparent_project(tmp_path, 'child', 'a')
    barrier = threading.Barrier(2)
    def close_a():
        barrier.wait()
        registry.begin_project_deletion(tmp_path, 'a')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close_a), pool.submit(move, ('child', 'other'))]
        for future in futures:
            future.result()
    assert rows(tmp_path)['child']['parent_id'] == 'other'
    # If closing wins, attaching a new child is refused; if attaching wins, Close promotes it.
    barrier = threading.Barrier(2)
    registry.create_project(tmp_path, 'new-parent')
    def close_new():
        barrier.wait()
        registry.begin_project_deletion(tmp_path, 'new-parent')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close_new), pool.submit(move, ('other', 'new-parent'))]
        result = futures[1].result()
        futures[0].result()
    assert result in {'saved', 'project_parent_inactive'}
    assert rows(tmp_path)['other']['parent_id'] == ''


def test_close_task_selector_does_not_follow_project_edges(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from supervisor import queue_transitions as transitions
    registry.create_project(tmp_path, 'parent')
    registry.create_project(tmp_path, 'child', parent_id='parent')
    registry.bind_task_to_project(tmp_path, 'child-root', 'child', origin={'absent': 'system'})
    queue = SimpleNamespace(_queue_lock=threading.RLock(), RUNNING={}, PENDING=[
        {'id': 'parent-root', 'project_id': 'parent'},
        {'id': 'parent-helper', 'parent_task_id': 'parent-root', 'root_task_id': 'parent-root'},
        {'id': 'child-root', 'project_id': 'child'},
        {'id': 'child-helper', 'parent_task_id': 'child-root', 'root_task_id': 'child-root'},
    ])
    monkeypatch.setattr(transitions, '_queue_module', lambda: queue)
    registry.begin_project_deletion(tmp_path, 'parent')
    assert set(transitions._live_project_task_ids(tmp_path, 'parent')) == {'parent-root', 'parent-helper'}
    assert set(transitions._live_project_task_ids(tmp_path, 'child')) == {'child-root', 'child-helper'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from ouroboros.gateway.router import collect_routes
    import ouroboros.gateway.projects as gateway
    monkeypatch.setattr(gateway, '_broadcast_projects_changed', lambda *args: None)
    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    app.state.repo_dir = pathlib.Path(__file__).parents[1]
    with TestClient(app) as http:
        yield http, tmp_path


def test_http_create_parent_update_catalog_schema_and_error_contract(client, monkeypatch):
    from tests.test_client_contract_bindings import assert_bound
    from ouroboros.gateway.schema import validate_ingress
    from ouroboros.gateway.contracts import ProjectCreateRequest
    from ouroboros.gateway.client_contracts import ProjectUpdateRequest
    http, root = client
    assert http.post('/api/projects', json={'id': 'parent'}).status_code == 200
    created = http.post('/api/projects', json={'id': 'child', 'parent_id': 'parent'})
    assert created.status_code == 200 and created.json()['project']['parent_id'] == 'parent'
    assert_bound('POST /api/projects', created)
    assert http.post('/api/projects', json={'id': 'child', 'parent_id': ''}).json()['project']['parent_id'] == 'parent'
    updated = http.post('/api/projects/CHILD/update', json={'parent_id': ''})
    assert updated.status_code == 200 and updated.json()['project']['parent_id'] == ''
    assert_bound('POST /api/projects/{project_id}/update', updated)
    assert http.post('/api/projects/child/update', json={'parent_id': 'parent'}).status_code == 200
    cycle = http.post('/api/projects/parent/update', json={'parent_id': 'child'})
    assert cycle.status_code == 409 and cycle.json()['reason_code'] == 'project_cycle'
    for body in ({'parent_id': None}, {'parent_id': 'Parent'}, {'parent_id': 'missing'},
                 {'parent_id': '', 'name': 'No'}, {'parent_id': '', 'notes': 'No'}):
        assert http.post('/api/projects/child/update', json=body).status_code == 400
    assert http.post('/api/projects/missing/update', json={'parent_id': ''}).status_code == 404
    assert http.post('/api/projects/child!/update', json={'parent_id': ''}).status_code == 400
    catalog = http.get('/api/projects/catalog?parent_id=parent&limit=1')
    assert catalog.json()['total'] == 1 and catalog.json()['projects'][0]['parent_path_complete']
    assert_bound('GET /api/projects/catalog', catalog)
    assert http.get('/api/projects/catalog?parent_id=').json()['total'] == 1
    assert http.get('/api/projects/catalog').json()['total'] == 2
    assert http.get('/api/projects/catalog?parent_id=Parent').status_code == 400
    assert_bound('GET /api/projects/{project_id}/detail', http.get('/api/projects/child/detail'))
    schema = http.get('/api/contract').json()
    assert schema['schemas']['ProjectCreateRequest']['properties']['parent_id'] == {'type': 'string'}
    assert schema['schemas']['ProjectUpdateRequest']['properties']['parent_id'] == {'type': 'string'}
    for cls in (ProjectCreateRequest, ProjectUpdateRequest):
        assert validate_ingress({'parent_id': None}, cls)
        assert validate_ingress({'parent_id': ''}, cls) == []
    # Invalid new-parent intent is rejected before cloning or attach initialization.
    import ouroboros.project_sources as sources
    monkeypatch.setattr(sources, 'clone_project_repo', lambda *a, **k: pytest.fail('must validate parent first'))
    bad = http.post('/api/projects', json={'id': 'bad', 'parent_id': 'missing', 'git_url': 'https://example.invalid/repo'})
    assert bad.status_code == 400 and registry.get_reserved_project(root, 'bad') is None
    registry.begin_project_deletion(root, 'parent')
    assert http.post('/api/projects/parent/update', json={'parent_id': ''}).status_code == 409
    assert http.post('/api/projects/child/update', json={'parent_id': 'parent'}).status_code == 409


def test_create_rechecks_parent_after_source_setup_without_deleting_prepared_resource(client, monkeypatch):
    import ouroboros.project_sources as sources
    http, root = client
    registry.create_project(root, 'parent')
    prepared = root / 'prepared-clone'
    def clone(*args):
        prepared.mkdir()
        (prepared / 'kept').write_text('prepared before parent closed')
        registry.begin_project_deletion(root, 'parent')
        return str(prepared), '', ''
    monkeypatch.setattr(sources, 'clone_project_repo', clone)
    result = http.post('/api/projects', json={'id': 'child', 'parent_id': 'parent',
                                            'git_url': 'https://example.invalid/repo'})
    assert result.status_code == 409 and result.json()['reason_code'] == 'project_parent_inactive'
    assert registry.get_reserved_project(root, 'child') is None
    assert (prepared / 'kept').read_text() == 'prepared before parent closed'


def test_healthy_detail_keeps_target_scoped_routing_validation(client):
    http, root = client
    registry.create_project(root, 'parent', name='Metadata parent')
    registry.create_project(root, 'healthy', parent_id='parent')
    data = json.loads(registry._registry_path(root).read_text(encoding='utf-8'))
    parent = next(row for row in data['projects'] if row['id'] == 'parent')
    parent['routing_generation'] = 'unreadable legacy routing'
    registry._save(root, data)
    before = registry._registry_path(root).read_bytes()
    result = http.get('/api/projects/healthy/detail')
    assert result.status_code == 200
    assert result.json()['project']['parent_path'] == [{'id': 'parent', 'name': 'Metadata parent'}]
    assert result.json()['project']['parent_path_complete'] is True
    assert registry._registry_path(root).read_bytes() == before
