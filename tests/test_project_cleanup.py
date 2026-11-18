"""Cleanup effects only in disposable roots; real local Git, never providers."""
import json
from pathlib import Path

import pytest
from ouroboros import projects_registry as r, project_footprint as f
from ouroboros.project_cleanup import cleanup_plan, cleanup_execute
from ouroboros.project_management import ProjectManagementError
from tests.test_repository_risk import git, repo


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'data'; system = tmp_path / 'system'; system.mkdir()
    r.create_project(root, 'alpha')
    def record(path, origin='project_created'):
        if origin == 'project_created':
            f.record_workspace_creation(root, 'alpha', str(path), 'fixture-base')
        else:
            f.record_target(root, 'alpha', str(path), task_id='writer', origin=origin, note='Disposable fixture')
        return path
    def plan(mode='complete'):
        return cleanup_plan(root, 'alpha', mode, system)
    def close():
        r.begin_project_deletion(root, 'alpha'); r.complete_project_deletion(root, 'alpha')
    def execute(targets=None, confirmations=None, mode='complete'):
        clean = {t['id']: t['selection_token'] for t in plan(mode)['targets'] if not t['warning']}
        return cleanup_execute(root, 'alpha', mode, targets if targets is not None else [t['id'] for t in plan(mode)['targets']], {**clean, **(confirmations or {})}, 'agent', system)
    return root, system, record, plan, close, execute


def test_created_temp_deleted_missing_idempotent_and_history(setup, tmp_path):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'env'); path.mkdir(); (path / 'temp').write_text('temp')
    targets = plan()['targets']; assert targets[0]['default_selected'] and targets[0]['warning']
    with pytest.raises(ProjectManagementError, match='fully closed'): execute()
    close(); target = plan()['targets'][0]
    result = execute(confirmations={target['id']: target['warning']['token']}); assert result['results'][0]['status'] == 'removed'
    assert not path.exists()
    assert execute()['results'][0]['status'] == 'already_absent'
    row = r.get_reserved_project(root, 'alpha')
    assert row['lifecycle'] == 'tombstoned' and len(row['cleanup_history']) == 6
    assert [h['phase'] for h in row['cleanup_history']] == ['started', 'target', 'finished'] * 2


def test_unpushed_warning_and_fresh_confirmation(setup, tmp_path):
    root, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); close()
    target = plan()['targets'][0]; assert target['warning']['unique_work']
    assert execute()['results'][0]['reason'] == 'current_warning_confirmation_required'
    old = target['warning']['token']; (path / 'file').write_text('changed')
    assert execute(confirmations={target['id']: old})['results'][0]['status'] == 'refused'
    fresh = plan()['targets'][0]
    result = execute(confirmations={fresh['id']: fresh['warning']['token']})
    assert result['results'][0]['status'] == 'removed' and not path.exists()


def test_runtime_shared_and_managed_aliases_are_never_deleted(setup, tmp_path):
    root, system, record, plan, close, execute = setup
    record(system); record(root)
    shared = record(tmp_path / 'shared'); shared.mkdir()
    r.create_project(root, 'beta', working_dir=str(shared))
    habitat = tmp_path / 'habitat'; habitat.mkdir()
    r.create_project(root, 'viper-discord', working_dir=str(habitat))
    alias = tmp_path / 'alias'; alias.symlink_to(habitat, target_is_directory=True); record(alias)
    close(); targets = plan()['targets']; assert all(t['protection'] for t in targets)
    assert all(t['status'] == 'refused' for t in execute()['results'])
    assert system.exists() and root.exists() and shared.exists() and alias.is_symlink()


def test_symlink_unlinks_without_following_and_parent_retains_child(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'keep').write_text('unique')
    alias = record(tmp_path / 'link'); alias.symlink_to(outside, target_is_directory=True)
    parent = record(tmp_path / 'parent'); parent.mkdir(); child = record(parent / 'keep'); child.mkdir()
    close(); targets = plan()['targets']; ids = {t['path']: t['id'] for t in targets}
    result = execute([ids[str(alias)], ids[str(parent)]], {t['id']: t['warning']['token'] for t in targets if t['warning']})
    assert not alias.is_symlink() and (outside / 'keep').read_text() == 'unique'
    assert next(t for t in result['results'] if t['path'] == str(parent))['reason'] == 'contains_retained_target'
    assert child.exists()


def test_reopen_friendly_saves_reconstruction_before_effect(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); git(path, 'remote', 'add', 'origin', 'https://example.invalid/repo')
    git(path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    close(); target = plan('reopen_friendly')['targets'][0]
    assert target['reconstruction']['head'] == git(path, 'rev-parse', 'HEAD')
    from ouroboros import project_cleanup as cleanup
    remove = cleanup._remove
    def check_before(t):
        row = r._load(root, strict=True)['projects'][0]
        assert row['cleanup_history'][-1]['targets'][0]['reconstruction']['remote_url']
        remove(t)
    monkeypatch.setattr(cleanup, '_remove', check_before)
    assert execute(mode='reopen_friendly')['results'][0]['status'] == 'removed'


def test_reopen_friendly_refuses_unreconstructible_repo_and_invalid_ids(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); close()
    target = plan('reopen_friendly')['targets'][0]
    assert target['retain_reason'] == 'reconstruction_unavailable'
    assert execute(mode='reopen_friendly')['results'][0]['status'] == 'refused'
    with pytest.raises(ProjectManagementError, match='Unknown target'): execute(['/etc'])
    assert path.exists()


def test_viper_interface_excluded(tmp_path):
    root = tmp_path / 'data'; r.create_project(root, 'viper-interface')
    with pytest.raises(ProjectManagementError, match='excluded'):
        cleanup_plan(root, 'viper-interface', system_repo_dir=tmp_path / 'system')


def test_linked_worktree_uses_git_removal_owner(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = repo(tmp_path); linked = record(tmp_path / 'linked')
    git(path, 'worktree', 'add', '-b', 'disposable', str(linked)); close()
    target = plan()['targets'][0]
    result = execute(confirmations={target['id']: target['warning']['token']})
    assert result['results'][0]['status'] == 'removed', result
    assert not linked.exists() and str(linked) not in git(path, 'worktree', 'list', '--porcelain')


def test_mount_refusal_and_never_delete_ancestors(setup, tmp_path, monkeypatch):
    _, _, record, plan, close, execute = setup
    path = record(tmp_path / 'mount-parent'); path.mkdir()
    from ouroboros.config import get_subagent_projects_root, get_deliverables_root
    record(Path.home()); record(Path(get_subagent_projects_root())); record(Path(get_deliverables_root()))
    from ouroboros import project_cleanup_context as custody
    monkeypatch.setattr(custody, 'mount_paths', lambda: [path / 'bound-child'])
    close(); targets = plan()['targets']; first = next(t for t in targets if t['path'] == str(path))
    result = execute([first['id']], {first['id']: first['warning']['token']})
    assert result['results'][0]['reason'] == 'mount_point_or_contains_mount'
    assert path.exists() and all(t['protection'] for t in targets if t['id'] != first['id'])


def test_lease_fences_reopen_second_cleanup_and_releases_global_lock(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'temp'); path.mkdir(); close(); target = plan()['targets'][0]
    from ouroboros import project_cleanup as cleanup, project_management as management
    remove = cleanup._remove
    def during_removal(t):
        import concurrent.futures
        def observe_other_thread():
            with r._file_write_lock(r._registry_path(root)):
                return True
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            assert pool.submit(observe_other_thread).result(timeout=2)
        with pytest.raises(ProjectManagementError, match='already running'): execute()
        with pytest.raises(ProjectManagementError, match='cleanup finishes'): management.reopen_project(root, 'alpha')
        remove(t)
    monkeypatch.setattr(cleanup, '_remove', during_removal)
    result = execute(confirmations={target['id']: target['warning']['token']})
    assert result['results'][0]['status'] == 'removed', result
    assert 'cleanup_lease' not in r.get_reserved_project(root, 'alpha')


def test_unknown_nonrepo_warns_and_replaced_inode_needs_new_confirmation(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = record(tmp_path / 'env'); path.mkdir(); close(); old = plan()['targets'][0]
    assert old['warning']['unknown']
    path.rename(tmp_path / 'old-env'); path.mkdir()
    result = execute(confirmations={old['id']: old['warning']['token']})
    assert result['results'][0]['reason'] == 'current_warning_confirmation_required'
    assert path.exists()


def test_url_userinfo_removed_in_reconstruction(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); git(path, 'remote', 'add', 'origin', 'https://user:secret@example.invalid/repo?token=secret')
    observed = plan('reopen_friendly')['targets'][0]
    assert observed['reconstruction']['remote_url'] == 'https://example.invalid/repo'
    assert 'secret' not in json.dumps(observed['reconstruction'])


def test_server_refuses_interface_close_and_cleanup_and_validates_body(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from ouroboros.gateway.router import collect_routes
    root = tmp_path / 'data'; r.create_project(root, 'viper-interface')
    r.create_project(root, 'alpha')
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = tmp_path / 'system'
    with TestClient(app) as client:
        assert client.post('/api/projects/viper-interface/delete', json={}).status_code == 403
        assert client.get('/api/projects/viper-interface/cleanup/plan').status_code == 403
        assert client.post('/api/projects/alpha/cleanup', json={}).status_code == 400
        assert client.post('/api/projects/alpha/cleanup', json={'mode': 'complete', 'selections': [], 'confirmations': {}}).status_code == 409
    assert r.get_reserved_project(root, 'viper-interface')['lifecycle'] == 'active'


def test_closed_shared_target_warns_but_active_shared_target_blocks(setup, tmp_path):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'shared'); path.mkdir()
    r.create_project(root, 'beta', working_dir=str(path)); r.begin_project_deletion(root, 'beta'); r.complete_project_deletion(root, 'beta')
    target = plan()['targets'][0]
    assert not target['protection'] and 'closed Project' in target['warning']['message']


def test_live_process_refuses_effect(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'live'); path.mkdir()
    r.bind_task_to_project(root, 'writer', 'alpha', origin={'absent': 'system'})
    from ouroboros import body_candidate
    monkeypatch.setattr(body_candidate, 'live_lineage_processes', lambda *args: [999])
    assert plan()['targets'][0]['protection'] == 'live_task_or_process_uses_target'


def test_partial_removal_is_not_success(setup, tmp_path, monkeypatch):
    _, _, record, plan, close, execute = setup
    path = record(tmp_path / 'stubborn'); path.mkdir(); close(); target = plan()['targets'][0]
    from ouroboros import project_cleanup
    monkeypatch.setattr(project_cleanup, '_remove', lambda t: None)
    assert execute(confirmations={target['id']: target['warning']['token']})['results'][0]['status'] == 'partially_removed'


def test_server_saved_close_request_and_boot_recovery(setup, tmp_path, monkeypatch):
    root, system, record, plan, _, _ = setup
    path = record(tmp_path / 'queued-temp'); path.mkdir()
    target = plan()['targets'][0]
    request = {'mode': 'complete', 'selections': [target['id']], 'confirmations': {target['id']: target['warning']['token']}, 'system_repo_dir': str(system)}
    from supervisor import queue_transitions as transitions
    monkeypatch.setattr(transitions, '_live_project_task_ids', lambda *args, **kw: set())
    monkeypatch.setattr(transitions, '_broadcast_projects_changed', lambda *args: None)
    r.begin_project_deletion(root, 'alpha', cleanup_request=request)
    # A restart can happen after the tombstone but before the saved cleanup runs.
    r.complete_project_deletion(root, 'alpha')
    with pytest.raises(ProjectManagementError, match='saved Close cleanup'):
        cleanup_execute(root, 'alpha', 'complete', request['selections'], request['confirmations'], 'human', system)
    resumed = []
    def start(drive, pid, chat):
        resumed.append(pid); transitions.run_project_deletion(drive, pid, chat); return True
    monkeypatch.setattr(transitions, 'start_project_deletion', start)
    assert transitions.resume_project_deletions(root) == 1 and resumed == ['alpha']
    row = r.get_reserved_project(root, 'alpha')
    assert row['cleanup_request']['status'] == 'finished' and not path.exists()
    assert transitions.resume_project_deletions(root) == 0


def test_stale_clean_git_confirmation_and_main_worktree_dependency(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); git(path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    close(); target = plan()['targets'][0]; assert not target['warning']
    (path / 'file').write_text('second'); git(path, 'add', '.'); git(path, 'commit', '-qm', 'second'); git(path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    result = execute(confirmations={target['id']: target['selection_token']})
    assert result['results'][0]['status'] == 'refused' and path.exists()
    git(path, 'worktree', 'add', '-b', 'other', str(tmp_path / 'other-worktree'))
    target = plan()['targets'][0]
    assert target['retain_reason'] == 'other_git_worktrees_depend_on_repository'


def test_stale_cleanup_lease_recoverable(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'temp'); path.mkdir(); close(); target = plan()['targets'][0]
    from ouroboros import project_cleanup_context as custody
    with r._file_write_lock(r._registry_path(root)):
        data = r._load(root, strict=True); data['projects'][0]['cleanup_lease'] = {'pid': 999999999, 'id': 'dead'}; r._save(root, data)
    assert not custody.lease_live({'pid': 999999999})
    assert execute(confirmations={target['id']: target['warning']['token']})['results'][0]['status'] == 'removed'


def test_newly_recorded_retained_child_prevents_ancestor_deletion(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    parent = record(tmp_path / 'parent'); parent.mkdir()
    trigger = record(tmp_path / 'deep' / 'trigger'); trigger.mkdir(parents=True)
    close(); targets = plan()['targets']
    from ouroboros import project_cleanup
    remove = project_cleanup._remove
    def concurrent_record(t):
        if t['path'] == str(trigger):
            child = parent / 'new-retained'; child.mkdir()
            record(child, 'pre_existing')
        remove(t)
    monkeypatch.setattr(project_cleanup, '_remove', concurrent_record)
    result = execute(confirmations={t['id']: t['warning']['token'] for t in targets if t['warning']})
    assert (parent / 'new-retained').exists(), result
    assert next(t for t in result['results'] if t['path'] == str(parent))['reason'] == 'contains_retained_target'


def test_copy_inventory_strict_read_only_and_owner_retirement(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'copy'); path.mkdir()
    r.bind_task_to_project(root, 'writer', 'alpha', origin={'absent': 'system'})
    from ouroboros import subagent_worktrees as wt, project_cleanup_context as custody
    legacy = wt._legacy_registry_path(root); legacy.parent.mkdir(parents=True, exist_ok=True)
    row = {'path': str(path), 'task_id': 'writer', 'kind': 'body_candidate'}
    legacy.write_text(json.dumps({'worktrees': [row]})); before = legacy.read_bytes()
    target = plan()['targets'][0]
    assert target['copy_record'] == row and legacy.read_bytes() == before
    assert not wt._records_dir(root).exists()  # planning did not migrate the old store
    seen = []
    monkeypatch.setattr(custody, 'retire_copy', lambda data, copy: seen.append(copy) or False)
    close(); result = execute(confirmations={target['id']: target['warning']['token']})
    assert seen == [row] and path.exists()
    assert result['results'][0]['reason'] == 'retirement_owner_retained_copy'
    current = wt._records_dir(root); current.mkdir(); (current / 'corrupt.json').write_text('{')
    with pytest.raises(ValueError): plan()
    assert legacy.read_bytes() == before and not (root / 'logs/events.jsonl').exists()


def test_task_paths_are_not_implicitly_disposable(setup, tmp_path):
    root, _, _, plan, _, _ = setup
    from ouroboros.task_results import write_task_result
    paths = {key: tmp_path / key for key in ('workspace_root', 'child_drive_root')}
    for path in paths.values(): path.mkdir()
    write_task_result(root, 'task', 'completed', project_id='alpha', **{key: str(path) for key, path in paths.items()})
    targets = plan()['targets']
    assert len(targets) == 2 and all(t['protection'] and not t['default_selected'] for t in targets)


def test_planning_does_not_create_or_modify_persistent_data(setup):
    root, _, _, plan, _, _ = setup
    def snapshot():
        return {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'current.sqlite-shm'}
    before = snapshot()
    plan()
    assert snapshot() == before
