"""Location CAS changes future defaults, not files, contextual paths or bound work."""
from __future__ import annotations

import copy
import pathlib
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ouroboros import project_management as m, projects_registry as r
from ouroboros.workspace_admission import room_chat_lens_dir, resolve_room_workspace


def change(root, pid, path, repo, basis=None):
    return m.update_project_location(root, pid, working_dir=str(path), system_repo_dir=repo,
        expected_location=basis or m.project_location(r.get_reserved_project(root, pid)))


def test_location_preserves_legacy_resources_history_and_unrelated_writers(tmp_path):
    root, repo = tmp_path / 'data', tmp_path / 'core'
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir(); new.mkdir(); repo.mkdir()
    r.create_project(root, 'a', working_dir=str(old))
    r.update_project(root, 'a', provenance='cloned', clone_url='https://example.invalid/repo')
    r.create_project(root, 'b', working_dir=str(old))
    before_b = r.get_reserved_project(root, 'b')
    expected = m.project_location(r.get_reserved_project(root, 'a'))
    r.update_project(root, 'a', name='Renamed', pinned=True)
    m.update_project_content(root, 'a', expected_content={'description': '', 'notes': ''}, notes='kept')
    view = change(root, 'a', new, repo, expected)
    assert view['name'] == 'Renamed' and view['pinned'] and view['notes'] == 'kept'
    assert view['clone_url'] == 'https://example.invalid/repo' and view['provenance'] == 'cloned'
    assert view['resources'][0]['path'] == str(old)
    assert r.get_reserved_project(root, 'b') == before_b
    assert list(old.iterdir()) == list(new.iterdir()) == []
    with pytest.raises(m.ProjectLocationConflict):
        change(root, 'a', old, repo, expected)
    stable = r._registry_path(root).read_bytes()
    change(root, 'a', new, repo)
    assert r._registry_path(root).read_bytes() == stable


def test_clear_is_explicit_folderless_and_does_not_provision(tmp_path, monkeypatch):
    root, repo, old = tmp_path / 'data', tmp_path / 'core', tmp_path / 'missing'
    repo.mkdir()
    r.create_project(root, 'a', working_dir=str(old))
    cleared = change(root, 'a', '', repo)
    assert cleared['working_dir'] == '' and cleared['working_dir_configured'] is True
    monkeypatch.setattr('ouroboros.subagent_worktrees.provision_genesis_project', lambda **kw: pytest.fail('must not provision'))
    assert r.ensure_project_workspace(root, 'a', repo) == ''
    assert resolve_room_workspace(drive_root=root, system_repo_dir=repo, project_id='a') == ('', '')
    assert room_chat_lens_dir(root, 'a') == ('', '')
    assert cleared['resources'][0]['path'] == str(old) and not old.exists()
    basis = r.project_admission_view(root, 'a')
    assert basis['project']['working_dir_configured'] is True


def test_bound_targets_and_preparation_fence(tmp_path):
    root, repo = tmp_path / 'data', tmp_path / 'core'
    old, new = tmp_path / 'old', tmp_path / 'new'
    for path in (repo, old, new): path.mkdir()
    r.create_project(root, 'a', working_dir=str(old))
    preparing = r.project_admission_view(root, 'a')
    bound = r.project_admission_view(root, 'a', frozen=True)
    retained = {'workspace_root': str(old), '_project_admission': copy.deepcopy(bound)}
    change(root, 'a', new, repo)
    with pytest.raises(r.ProjectAdmissionError, match='changed'):
        with r.project_admission_guard(root, preparing): pass
    with r.project_admission_guard(root, bound): pass
    assert retained['workspace_root'] == str(old) and retained['_project_admission'] == bound
    assert room_chat_lens_dir(root, 'a', project_admission=bound) == (str(old), '')
    assert room_chat_lens_dir(root, 'a') == (str(new), '')
    assert resolve_room_workspace(drive_root=root, system_repo_dir=repo, project_id='a') == (str(new), '')
    assert resolve_room_workspace(drive_root=root, system_repo_dir=repo, project_id='a', explicit_workspace=str(old)) == (str(old), '')
    assert resolve_room_workspace(drive_root=root, system_repo_dir=repo, project_id='a', workspace_sentinel='none') == ('', '')
    change(root, 'a', '', repo)
    assert room_chat_lens_dir(root, 'a', project_admission=bound) == (str(old), '')


@pytest.mark.parametrize('bad', ['missing', 'file', 'core', 'data', 'symlink'])
def test_path_refusal_no_write(tmp_path, bad):
    root, repo = tmp_path / 'data', tmp_path / 'core'
    repo.mkdir(); r.create_project(root, 'a')
    target = tmp_path / bad
    if bad == 'file': target.write_text('unchanged', encoding='utf-8')
    if bad == 'symlink': target.symlink_to(repo, target_is_directory=True)
    before = r._registry_path(root).read_bytes()
    with pytest.raises(m.ProjectManagementError): change(root, 'a', target, repo)
    assert r._registry_path(root).read_bytes() == before


def test_existing_git_root_not_subdirectory_and_no_git_changes(tmp_path):
    folder, repo, root = tmp_path / 'checkout', tmp_path / 'core', tmp_path / 'data'
    folder.mkdir(); repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(folder)], check=True)
    child = folder / 'subdir'; child.mkdir()
    r.create_project(root, 'a')
    before = (folder / '.git' / 'HEAD').read_bytes()
    assert change(root, 'a', folder, repo)['working_dir'] == str(folder)
    with pytest.raises(m.ProjectManagementError): change(root, 'a', child, repo)
    assert (folder / '.git' / 'HEAD').read_bytes() == before
    assert not (folder / '.git' / 'index').exists()


def test_detail_default_independent_shared_probe_missing_unavailable(tmp_path, monkeypatch):
    root = tmp_path / 'data'; folder = str(tmp_path / 'missing')
    r.create_project(root, 'a', working_dir=folder)
    calls = []
    original = m.observe_resource
    def observe(path): calls.append(path); return original(path)
    monkeypatch.setattr(m, 'observe_resource', observe)
    view = m.project_detail(root, 'a')
    assert calls == [folder]
    assert view['working_dir_observation']['status'] == 'missing'
    m.detach_project_resource(root, 'a', 'default'); calls.clear()
    view = m.project_detail(root, 'a')
    assert calls == [folder] and view['resources'] == []
    monkeypatch.setattr(m, 'observe_resource', lambda path: {'status': 'unavailable', 'kind': 'unknown', 'git': {'status': 'unavailable', 'branch': '', 'head': '', 'root': ''}})
    assert m.project_detail(root, 'a')['working_dir_observation']['status'] == 'unavailable'


def test_http_location_cas_closed_reopen_and_mixed_refusals(tmp_path, monkeypatch):
    from ouroboros.gateway.router import collect_routes
    import ouroboros.gateway.projects as gateway
    from tests.test_client_contract_bindings import assert_bound
    root, folder = tmp_path / 'data', tmp_path / 'plain'; folder.mkdir()
    r.create_project(root, 'a')
    monkeypatch.setattr(gateway, '_broadcast_projects_changed', lambda *args: None)
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = tmp_path / 'core'
    with TestClient(app) as client:
        basis = client.get('/api/projects/a/detail').json()['project']['location']
        payload = {'working_dir': str(folder), 'expected_location': basis}
        response = client.post('/api/projects/a/update', json=payload)
        assert response.status_code == 200 and assert_bound('POST /api/projects/{project_id}/update', response)
        stale = client.post('/api/projects/a/update', json=payload)
        assert stale.status_code == 409 and stale.json()['current_location']['working_dir'] == str(folder)
        assert_bound('POST /api/projects/{project_id}/update', stale)
        assert client.post('/api/projects/a/update', json={**payload, 'notes': 'bad'}).status_code == 400
        r.begin_project_deletion(root, 'a'); r.complete_project_deletion(root, 'a')
        closed = client.get('/api/projects/a/detail').json()['project']
        saved = client.post('/api/projects/a/update', json={'working_dir': '', 'expected_location': closed['location']})
        assert saved.status_code == 200 and saved.json()['project']['lifecycle'] == 'tombstoned'
        reopened = client.post('/api/projects/a/reopen', json={})
        assert reopened.status_code == 200 and reopened.json()['project']['working_dir'] == ''
        assert not list(folder.iterdir())


def test_promoted_defaults_respect_selected_folderless_and_explicit_targets(tmp_path, monkeypatch):
    import supervisor.workers as workers
    from tests.test_promote_chat_flow import _promote_ctx
    root, repo = tmp_path / 'data', tmp_path / 'core'; repo.mkdir()
    first, second = tmp_path / 'first', tmp_path / 'second'; first.mkdir(); second.mkdir()
    monkeypatch.setattr(workers, 'DRIVE_ROOT', root)
    row = r.create_project(root, 'a', working_dir=str(first))
    change(root, 'a', second, repo)
    def promote(tid, **extra):
        enqueued = []
        result = workers.promote_chat_to_task({'type': 'promote_chat_to_task', 'task_id': tid,
            'objective': 'Read current location', 'project_id': 'a', 'chat_id': row['chat_id'], **extra}, _promote_ctx(enqueued))
        assert result['status'] == 'scheduled', result
        return enqueued[0]
    bound = promote('location-before-clear')
    assert bound['workspace_root'] == str(second)
    change(root, 'a', '', repo)
    monkeypatch.setattr('ouroboros.subagent_worktrees.provision_genesis_project', lambda **kw: pytest.fail('must not provision'))
    folderless = promote('location-after-clear')
    assert not folderless.get('workspace_root') and 'workspace_autoprovisioned' not in folderless['metadata']
    explicit = promote('location-explicit', workspace_root=str(first))
    assert explicit['workspace_root'] == str(first)
    assert r.get_project(root, 'a')['working_dir'] == '', 'explicit task must not undo the selected empty default'
    assert bound['workspace_root'] == str(second)


def test_fresh_process_keeps_selected_location_and_clear(tmp_path):
    import json
    import sys
    root, repo, folder = tmp_path / 'data', tmp_path / 'core', tmp_path / 'folder'
    repo.mkdir(); folder.mkdir(); r.create_project(root, 'a')
    source = pathlib.Path(__file__).parents[1]
    script = "import json,sys; sys.path.insert(0,sys.argv[1]); from ouroboros.project_management import project_detail; print(json.dumps(project_detail(sys.argv[2], 'a')))"
    def fresh():
        return json.loads(subprocess.run([sys.executable, '-c', script, str(source), str(root)],
            check=True, capture_output=True, text=True, cwd=source).stdout)
    selected = change(root, 'a', folder, repo)
    assert fresh()['location'] == selected['location']
    cleared = change(root, 'a', '', repo)
    assert fresh()['location'] == cleared['location'] and fresh()['working_dir_configured'] is True
    assert list(folder.iterdir()) == []
