"""Footprint producers persist facts without owning or copying resource trees."""
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient
from ouroboros import projects_registry as r, project_management as m, project_footprint as f
from ouroboros.gateway.router import collect_routes
from tests.test_repository_risk import repo, git


def test_first_observation_preserved_through_change_detach_reopen(tmp_path):
    root = tmp_path / 'data'; path = repo(tmp_path)
    r.create_project(root, 'alpha', working_dir=str(path))
    first = f.view(r.get_reserved_project(root, 'alpha'))['entries'][0]
    old_head = first['observation']['git']['head']
    (path / 'file').write_text('later'); git(path, 'commit', '-qam', 'later')
    r.create_project(root, 'alpha', working_dir=str(path))
    assert f.view(r.get_reserved_project(root, 'alpha'))['entries'][0] == first
    record = f.record_target(root, 'alpha', str(tmp_path / 'env'), task_id='task-a', origin='project_created', note='Explicit generated test environment')
    assert record['observation']['status'] == 'missing'  # declaration is not fabricated existence
    (tmp_path / 'env').mkdir()
    assert f.record_target(root, 'alpha', str(tmp_path / 'env'), task_id='task-a', origin='project_created', note='Explicit generated test environment') == record
    m.detach_project_resource(root, 'alpha', 'default')
    r.begin_project_deletion(root, 'alpha'); r.complete_project_deletion(root, 'alpha')
    assert f.inspect_target(root, 'alpha', first['id'])['facts']['head'] != old_head
    m.reopen_project(root, 'alpha')
    assert f.view(r.get_reserved_project(root, 'alpha'))['entries'] == [first, record]


def test_legacy_shared_missing_alias_and_no_tree_enumeration(tmp_path, monkeypatch):
    root = tmp_path / 'data'; path = tmp_path / 'preexisting'; path.mkdir()
    (path / 'keep').write_text('untouched')
    r.create_project(root, 'alpha', working_dir=str(path)); r.create_project(root, 'beta')
    # Genuine legacy state: no historical record, not today's state backfilled.
    data = r._load(root, strict=True, project_id='alpha'); data['projects'][0].pop('footprint'); r._save(root, data)
    def no_walk(*a, **k): raise AssertionError('no filesystem content enumeration')
    monkeypatch.setattr(Path, 'rglob', no_walk)
    before = (path / 'keep').read_bytes()
    m.add_project_resource(root, 'beta', str(path))
    alias = tmp_path / 'alias'; alias.symlink_to(path, target_is_directory=True)
    m.add_project_resource(root, 'beta', str(alias))
    m.add_project_resource(root, 'beta', str(tmp_path / 'missing'))
    assert f.view(r.get_reserved_project(root, 'alpha'))['legacy_unknown_paths'] == [str(path)]
    assert len(f.view(r.get_reserved_project(root, 'beta'))['entries']) == 3
    assert before == (path / 'keep').read_bytes()
    assert f.view(r.get_reserved_project(root, 'beta'))['entries'][0]['origin'] == 'unknown'


def test_real_http_and_tool_consumers(tmp_path):
    from ouroboros.tools.project_resources import _project_resources
    from ouroboros.tools.registry import ToolContext
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    path = repo(workspace); root = tmp_path / 'data'
    r.create_project(root, 'alpha', working_dir=str(path))
    ctx = ToolContext(repo_dir=Path(__file__).parents[1], drive_root=root, task_id='writer')
    ctx.workspace_root = workspace
    ctx.workspace_mode = 'external'
    ctx.project_id = 'alpha'
    ctx.task_metadata = {'delegation_role': 'root', 'root_task_id': 'writer'}
    result = _project_resources(ctx, 'record', 'alpha', path=str(workspace / 'explicit-output'), origin='project_created', note='Generated output declaration')
    assert 'agent_declaration' in result, result
    result = json.loads(_project_resources(ctx, 'footprint', 'alpha'))
    record = result['footprint']['entries'][-1]
    assert record['task_id'] == 'writer'
    observed = json.loads(_project_resources(ctx, 'inspect', 'alpha', resource_id='default'))
    assert not observed['deletion_authorized'] and observed['arrangement'] == 'main'
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = Path(__file__).parents[1]
    before = r._registry_path(root).read_bytes()
    with TestClient(app) as client:
        view = client.get('/api/projects/alpha/detail?resource_preview=1')
        assert view.status_code == 200, view.text
        assert view.json()['resource_preview']['footprint']['entries'][-1] == record
        assert client.get('/api/projects/alpha/detail?inspect_resource=default').json()['resource_inspection']['facts']['head']
        assert client.get('/api/projects/alpha/detail?inspect_resource=/etc').status_code == 404
    assert r._registry_path(root).read_bytes() == before


def test_generated_projection_uses_existing_records_without_target_walk(tmp_path, monkeypatch):
    from ouroboros.task_results import write_task_result
    root = tmp_path / 'data'; r.create_project(root, 'alpha')
    r.bind_task_to_project(root, 'task-a', 'alpha', origin={'absent': 'system'})
    write_task_result(root, 'task-a', 'completed', project_id='alpha', child_drive_root='/missing/old-drive', artifacts=[{'path': '/missing/output'}])
    records = root / 'state/private_copies'; records.mkdir(parents=True)
    (records / 'copy.json').write_text(json.dumps({'path': '/missing/copy', 'task_id': 'task-a', 'kind': 'body_candidate'}))
    (records / 'other.json').write_text(json.dumps({'path': '/unrelated', 'task_id': 'task-b'}))
    monkeypatch.setattr(Path, 'rglob', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no content walk')))
    observed = f.generated_targets(root, 'alpha')
    paths = {row['path'] for row in observed['targets']}
    assert paths == {'/missing/old-drive', '/missing/output', '/missing/copy'}
    assert not observed['gaps']


def test_source_fact_stays_bound_to_original_location_and_genesis_is_recorded(tmp_path, monkeypatch):
    root = tmp_path / 'data'; old = tmp_path / 'old'; old.mkdir()
    r.create_project(root, 'alpha', working_dir=str(old))
    r.update_project(root, 'alpha', provenance='cloned', clone_url='https://example.invalid/repo')
    other = tmp_path / 'other'; other.mkdir()
    monkeypatch.setattr('ouroboros.project_sources.validate_attach_path', lambda *a, **k: (other, ''))
    monkeypatch.setattr('ouroboros.workspace_admission.validate_workspace_root', lambda *a, **k: other)
    m.update_project_location(root, 'alpha', working_dir=str(other), expected_location=m.project_location(r.get_reserved_project(root, 'alpha')), system_repo_dir=tmp_path / 'system')
    facts = f.view(r.get_reserved_project(root, 'alpha'))['entries']
    assert [v['path'] for v in facts if v['origin'] == 'cloned'] == [str(old)]
    assert facts[-1]['path'] == str(other) and facts[-1]['origin'] == 'pre_existing'
    # The real provisioning producer returns the seed, which the footprint keeps.
    project = r.ensure_project_workspace(root, 'new-room', Path(__file__).parents[1], return_project=True)
    entry = next(v for v in f.view(r.get_reserved_project(root, 'new-room'))['entries'] if v['source'] == 'genesis_producer')
    assert entry['base_sha'] and entry['path'] == project['working_dir']
    assert entry['observation']['git']['head'] == entry['base_sha']


def test_tool_add_is_context_only_and_inspection_keeps_resource_boundary(tmp_path, monkeypatch):
    from ouroboros.tools.project_resources import _project_resources
    from ouroboros.tools.registry import ToolContext
    workspace = tmp_path / 'work'; workspace.mkdir()
    root = tmp_path / 'data'; r.create_project(root, 'alpha')
    ctx = ToolContext(repo_dir=Path(__file__).parents[1], drive_root=root, task_id='writer', workspace_root=workspace, workspace_mode='external')
    ctx.project_id = 'alpha'; ctx.task_metadata = {'delegation_role': 'root'}
    monkeypatch.setattr(m, 'observe_resource', lambda path: (_ for _ in ()).throw(AssertionError('association must not probe')))
    assert 'outside' in _project_resources(ctx, 'add', 'alpha', path='/outside/unavailable')
    rid = m.list_project_resources(root, 'alpha')[0]['id']
    before = r._registry_path(root).read_bytes()
    refused = _project_resources(ctx, 'inspect', 'alpha', resource_id=rid)
    assert 'outside selected root' in refused
    assert r._registry_path(root).read_bytes() == before


def test_explicit_git_initialization_records_only_its_before_target(tmp_path, monkeypatch):
    import ouroboros.gateway.projects as gateway
    monkeypatch.setattr(gateway, '_broadcast_projects_changed', lambda *args: None)
    root = tmp_path / 'data'; folder = tmp_path / 'existing'; folder.mkdir()
    (folder / 'existing.txt').write_text('pre-existing owner material')
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = Path(__file__).parents[1]
    with TestClient(app) as client:
        response = client.post('/api/projects', json={'id': 'new-project', 'name': 'New', 'path': str(folder), 'init_git': True})
        assert response.status_code == 200, response.text
    records = f.view(r.get_reserved_project(root, 'new-project'))['entries']
    initialized = next(record for record in records if record['source'] == 'git_initialized')
    assert initialized['path'] == str(folder / '.git')
    assert initialized['observation']['status'] == 'missing'
    assert (folder / '.git').exists() and (folder / 'existing.txt').read_text() == 'pre-existing owner material'


def test_compact_views_and_generated_id_inspection(tmp_path):
    from ouroboros.task_results import write_task_result
    root = tmp_path / 'data'; path = repo(tmp_path)
    r.create_project(root, 'alpha', working_dir=str(path))
    write_task_result(root, 'task-a', 'completed', project_id='alpha', artifacts=[{'path': str(path)}])
    generated = f.generated_targets(root, 'alpha')['targets'][0]
    assert generated['id'].startswith('generated:')
    assert f.inspect_target(root, 'alpha', generated['id'])['facts']['head']
    assert 'footprint' not in m.project_catalog(root)['projects'][0]
    assert 'footprint' not in m.project_detail(root, 'alpha')
    before = r._registry_path(root).read_bytes()
    assert f.generated_targets(root, 'alpha')['targets'][0]['id'] == generated['id']
    assert before == r._registry_path(root).read_bytes()


def test_outside_declaration_never_acquires_read_rights(tmp_path, monkeypatch):
    from ouroboros.tools.project_resources import _project_resources
    from ouroboros.tools.registry import ToolContext
    workspace = tmp_path / 'work'; workspace.mkdir()
    root = tmp_path / 'data'; r.create_project(root, 'alpha')
    ctx = ToolContext(repo_dir=Path(__file__).parents[1], drive_root=root, task_id='writer', workspace_root=workspace, workspace_mode='external')
    ctx.project_id = 'alpha'; ctx.task_metadata = {'delegation_role': 'root'}
    monkeypatch.setattr(m, 'observe_resource', lambda path: (_ for _ in ()).throw(AssertionError('no read authority')))
    record = json.loads(_project_resources(ctx, 'record', 'alpha', path='/outside/generated-environment', origin='project_created'))
    assert record['observation'] is None and record['observation_error']
    assert record['source'] == 'agent_declaration'
    assert 'outside selected root' in _project_resources(ctx, 'inspect', 'alpha', resource_id=record['id'])


def test_real_private_copy_producer_contract_is_consumed(tmp_path):
    from ouroboros.subagent_worktrees import provision_worktree
    from ouroboros.task_results import write_task_result
    root = tmp_path / 'data'; source = repo(tmp_path)
    r.create_project(root, 'alpha')
    write_task_result(root, 'copy-task', 'completed', project_id='alpha')
    handle = provision_worktree(repo_dir=source, task_id='copy-task', worktree_root=tmp_path / 'copies', data_dir=root, source_is_system_repo=False)
    record_files = {p: p.read_bytes() for p in (root / 'state/private_copies').glob('*.json')}
    target = next(row for row in f.generated_targets(root, 'alpha')['targets'] if row['source'] == 'private_copy_record')
    assert target['path'] == handle.path and target['task_id'] == 'copy-task'
    assert target['base_sha'] == handle.base_sha
    assert f.inspect_target(root, 'alpha', target['id'])['arrangement'] == 'linked'
    assert record_files == {p: p.read_bytes() for p in record_files}


def test_tool_inspect_recorded_private_copy_preserves_record_confinement(tmp_path, monkeypatch):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.subagent_worktrees import provision_worktree
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.project_resources import _project_resources
    from ouroboros.tools.registry import ToolContext
    root = tmp_path / 'data'
    home = tmp_path / 'home'; home.mkdir()
    workspace = tmp_path / 'work'; workspace.mkdir()
    copies_root = tmp_path / 'copies'
    monkeypatch.setenv('OUROBOROS_USER_FILES_ROOT', str(home))
    monkeypatch.setenv('OUROBOROS_SUBAGENT_WORKTREE_ROOT', str(copies_root))
    source = repo(tmp_path)
    r.create_project(root, 'alpha', working_dir=str(workspace))
    write_task_result(root, 'copy-task', 'completed', project_id='alpha')
    handle = provision_worktree(repo_dir=source, task_id='copy-task', worktree_root=copies_root, data_dir=root, source_is_system_repo=False)
    target = next(row for row in f.generated_targets(root, 'alpha')['targets'] if row['source'] == 'private_copy_record')
    ctx = ToolContext(repo_dir=Path(__file__).parents[1], drive_root=root, task_id='root-inspector',
                      workspace_root=workspace, workspace_mode='external', is_direct_chat=True)
    ctx.project_id = 'alpha'; ctx.task_metadata = {'delegation_role': 'root'}
    inspected = json.loads(_project_resources(ctx, 'inspect', 'alpha', resource_id=target['id']))
    assert inspected['arrangement'] == 'linked' and inspected['status'] == 'repository'
    # Recording an unrecorded directory inside copies_root must NOT acquire read/inspect authority.
    unrecorded = copies_root / 'copies.noindex' / 'unrecorded'; unrecorded.mkdir(parents=True)
    rec = json.loads(_project_resources(ctx, 'record', 'alpha', path=str(unrecorded), origin='project_created'))
    assert rec['observation'] is None and rec['observation_error']
    assert 'Unrecorded path' in _project_resources(ctx, 'inspect', 'alpha', resource_id=rec['id'])
    # Subagents remain blocked from inspecting sibling/unassigned copies under OUROBOROS_SUBAGENT_WORKTREE_ROOT,
    # while an acting_subagent inspecting its OWN active_workspace copy keeps its existing read access.
    ctx.is_direct_chat = False; ctx.task_metadata = {'parent_task_id': 'parent', 'delegation_role': 'subagent'}
    ctx.task_constraint = TaskConstraint(mode='local_readonly_subagent')
    assert 'ordinary root' in _project_resources(ctx, 'inspect', 'alpha', resource_id=target['id'])
    ctx.workspace_root = Path(handle.path)
    ctx.workspace_mode = 'self_worktree'
    ctx.task_constraint = TaskConstraint(mode='acting_subagent', surface='self_worktree', write_root=handle.path)
    own_inspected = json.loads(_project_resources(ctx, 'inspect', 'alpha', resource_id=target['id']))
    assert own_inspected['arrangement'] == 'linked' and own_inspected['status'] == 'repository'
