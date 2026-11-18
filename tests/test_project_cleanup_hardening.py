"""Adversarial cleanup regressions; every effect stays in pytest temporary roots."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from ouroboros import projects_registry as r, project_footprint as f
from ouroboros import project_cleanup as cleanup, project_cleanup_context as custody
from ouroboros.project_management import ProjectManagementError
from tests.test_project_cleanup import setup
from tests.test_repository_risk import git, repo


def confirmed(plan):
    return {t['id']: (t['warning'] or {}).get('token', t['selection_token']) for t in plan['targets']}


def test_nested_repository_external_worktree_survives(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    parent = record(tmp_path / 'container'); parent.mkdir()
    main = repo(parent); linked = tmp_path / 'linked'
    git(main, 'worktree', 'add', '-b', 'linked', str(linked)); close()
    target = plan()['targets'][0]
    assert target['retain_reason'] == 'other_git_worktrees_depend_on_repository'
    assert execute(confirmations=confirmed(plan()))['results'][0]['status'] == 'refused'
    assert main.exists() and git(linked, 'rev-parse', 'HEAD')


def test_failed_worktree_inventory_retained(setup, tmp_path, monkeypatch):
    _, _, record, plan, close, execute = setup
    path = record(repo(tmp_path)); close()
    inspect = cleanup.inspect_repository
    def unknown(path):
        risk = inspect(path); risk['facts']['worktrees'] = None; risk['gaps'].append('worktree failed'); risk['status'] = 'partial'
        return risk
    monkeypatch.setattr(cleanup, 'inspect_repository', unknown)
    assert plan()['targets'][0]['retain_reason'] == 'git_worktree_inventory_unknown'
    assert execute(confirmations=confirmed(plan()))['results'][0]['status'] == 'refused'
    assert path.exists()


def test_nested_scan_caps_and_no_symlink_traversal(setup, tmp_path, monkeypatch):
    _, _, record, plan, _, _ = setup
    path = record(tmp_path / 'container'); path.mkdir()
    (path / 'a').mkdir(); (path / 'a' / 'b').mkdir()
    monkeypatch.setattr(custody, 'NESTED_SCAN_DEPTH', 1)
    assert plan()['targets'][0]['retain_reason'] == 'nested_repository_inventory_unknown'
    (path / 'a' / 'b').rmdir(); (path / 'a').rmdir()
    outside = tmp_path / 'outside'; outside.mkdir(); repo(outside)
    (path / 'alias').symlink_to(outside, target_is_directory=True)
    assert not plan()['targets'][0]['retain_reason']
    monkeypatch.setattr(custody, 'NESTED_SCAN_ENTRIES', 0)
    assert plan()['targets'][0]['retain_reason'] == 'nested_repository_inventory_unknown'


def test_authored_created_label_is_not_producer_evidence(setup, tmp_path):
    root, _, _, plan, _, _ = setup
    path = tmp_path / 'declared'; path.mkdir()
    f.record_target(root, 'alpha', str(path), task_id='author', origin='project_created')
    target = plan()['targets'][0]
    assert not target['default_selected'] and target['kind'] == 'attached_external' and target['warning']
    f.record_workspace_creation(root, 'alpha', str(path), 'base')
    assert plan()['targets'][0]['default_selected']


@pytest.mark.parametrize('pid', sorted(custody.MANAGED_PROJECT_IDS))
def test_all_managed_ids_refused(tmp_path, pid):
    root = tmp_path / 'data'; r.create_project(root, pid)
    with pytest.raises(ProjectManagementError, match='excluded'):
        cleanup.cleanup_plan(root, pid, system_repo_dir=tmp_path / 'system')
    from ouroboros.project_management import project_detail
    assert project_detail(root, pid)['cleanup_allowed'] is False
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from ouroboros.gateway.router import collect_routes
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = tmp_path / 'system'
    with TestClient(app) as client:
        assert client.get(f'/api/projects/{pid}/cleanup/plan').status_code == 403
        assert client.post(f'/api/projects/{pid}/delete', json={}).status_code == 403
        assert client.post(f'/api/projects/{pid}/cleanup', json={'mode': 'complete', 'selections': [], 'confirmations': {}}).status_code == 403


def test_git_inspection_outside_lock_and_linear(setup, tmp_path, monkeypatch):
    root, system, record, plan, close, execute = setup
    for n in range(4):
        base = tmp_path / str(n); base.mkdir(); record(repo(base))
    close(); tokens = confirmed(plan()); inspections = []
    inspect = cleanup.inspect_repository
    def checked(path):
        import threading
        acquired = threading.Event()
        def acquire():
            with r._file_write_lock(r._registry_path(root)): acquired.set()
        thread = threading.Thread(target=acquire, daemon=True); thread.start()
        assert acquired.wait(1), 'Git inspection held registry lock'
        thread.join(1)
        inspections.append(path)
        return inspect(path)
    monkeypatch.setattr(cleanup, 'inspect_repository', checked)
    result = cleanup.cleanup_execute(root, 'alpha', 'complete', list(tokens), tokens, 'agent', system)
    assert all(t['status'] == 'removed' for t in result['results']), result
    assert len(inspections) == 8  # execution inspection + one fresh inspection/target, not N full replans


@pytest.mark.parametrize('error', [OSError('rmtree failed'), RuntimeError('unexpected owner error')])
def test_target_exception_finishes_and_continues(setup, tmp_path, monkeypatch, error):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'partial'); path.mkdir(); (path / 'one').write_text('one'); (path / 'two').write_text('two')
    second = record(tmp_path / 'second'); second.mkdir(); close()
    original = cleanup._remove
    def partial(t):
        if t['path'] == str(path):
            (path / 'one').unlink(); raise error
        original(t)
    monkeypatch.setattr(cleanup, '_remove', partial)
    result = execute(confirmations=confirmed(plan()))
    outcome = next(t for t in result['results'] if t['path'] == str(path))
    assert outcome['status'] == 'partially_removed' and str(error) in outcome['reason']
    assert not second.exists() and (path / 'two').exists()
    row = r.get_reserved_project(root, 'alpha')
    assert row['cleanup_history'][-1]['phase'] == 'finished' and 'cleanup_lease' not in row


@pytest.mark.skipif(not Path('/proc').is_dir(), reason='Linux proc cwd observation')
def test_readable_process_cwd_refuses(setup, tmp_path):
    _, _, record, plan, close, execute = setup
    path = record(tmp_path / 'busy'); path.mkdir()
    proc = subprocess.Popen(['sleep', '30'], cwd=path)
    try:
        close(); target = plan()['targets'][0]
        assert target['retain_reason'] == 'process_cwd_inside'
        assert execute(confirmations=confirmed(plan()))['results'][0]['status'] == 'refused'
        assert path.exists()
    finally:
        proc.terminate(); proc.wait(timeout=5)


def save_pending(root, system, **extra):
    request = {'mode': 'complete', 'selections': [], 'confirmations': {}, 'system_repo_dir': str(system)}
    r.begin_project_deletion(root, 'alpha', cleanup_request=request); r.complete_project_deletion(root, 'alpha')
    if extra:
        with r._file_write_lock(r._registry_path(root)):
            data = r._load(root, strict=True); data['projects'][0].update(extra); r._save(root, data)


@pytest.mark.parametrize('present', [True, False])
def test_pending_cleanup_without_saved_request_is_noop(setup, monkeypatch, present):
    root, *_ = setup
    def unexpected(*args, **kwargs):
        pytest.fail('cleanup without a saved request must not execute or save')
    monkeypatch.setattr(cleanup, 'cleanup_execute', unexpected)
    monkeypatch.setattr(r, '_save', unexpected)
    if not present:
        monkeypatch.setattr(r, '_load', lambda *args, **kwargs: {'projects': []})
    custody.run_pending_cleanup(root, 'alpha')


@pytest.mark.parametrize('gone,birth,expected', [(True, 'same', False), (False, 'same', True), (False, 'other', False), (False, '', True)])
def test_cleanup_lease_uses_positive_platform_absence(monkeypatch, gone, birth, expected):
    monkeypatch.setattr(custody, 'pid_provably_gone', lambda pid: gone)
    monkeypatch.setattr(custody, 'process_kernel_birth', lambda pid: birth)
    assert custody.lease_live({'pid': 42, 'birth': 'same'}) is expected


@pytest.mark.parametrize('raises', [True, False])
def test_pending_unexpected_exception_settled_and_broadcast(setup, monkeypatch, raises):
    root, system, *_ = setup; save_pending(root, system)
    def fail(*a, **k):
        if raises: raise RuntimeError('owner adapter failure')
        return {'error': 'owner adapter failure'}
    monkeypatch.setattr(cleanup, 'cleanup_execute', fail)
    from supervisor import queue_transitions
    broadcasts = []; monkeypatch.setattr(queue_transitions, '_broadcast_projects_changed', lambda *a: broadcasts.append(a))
    custody.run_pending_cleanup(root, 'alpha')
    request = r.get_reserved_project(root, 'alpha')['cleanup_request']
    assert request['status'] == 'failed' and 'owner adapter failure' in request['result']['error']
    assert broadcasts


def test_live_lease_pending_is_deferred_and_broadcast(setup, monkeypatch):
    root, system, *_ = setup; save_pending(root, system, cleanup_lease=custody.lease('busy'))
    from supervisor import queue_transitions
    broadcasts = []; monkeypatch.setattr(queue_transitions, '_broadcast_projects_changed', lambda *a: broadcasts.append(a))
    custody.run_pending_cleanup(root, 'alpha')
    request = r.get_reserved_project(root, 'alpha')['cleanup_request']
    assert request['status'] == 'deferred' and request['result']['error'] and broadcasts


def test_clear_pending_endpoint_and_dropped_close_request(setup):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from ouroboros.gateway.router import collect_routes
    root, system, *_ = setup; save_pending(root, system)
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = system
    with TestClient(app) as client:
        response = client.post('/api/projects/alpha/delete', json={'cleanup': {'mode': 'complete', 'selections': [], 'confirmations': {}}})
        assert response.status_code == 409 and response.json()['folder_untouched'] is True and response.json()['cleanup_accepted'] is False
        response = client.post('/api/projects/alpha/cleanup', json={'action': 'clear_pending'})
        assert response.status_code == 200, response.text
        saved = r.get_reserved_project(root, 'alpha')['cleanup_request']
        assert saved['status'] == 'cancelled' and 'error' not in saved['result']
        response = client.post('/api/projects/alpha/cleanup', json={'mode': 'complete', 'selections': [], 'confirmations': {}})
        assert response.status_code == 200, response.text
        from ouroboros.project_management import reopen_project
        assert reopen_project(root, 'alpha')['lifecycle'] == 'active'


def test_fixed_roots_follow_configuration(tmp_path, monkeypatch):
    from ouroboros import config
    monkeypatch.setattr(config, 'get_subagent_worktree_root', lambda: str(tmp_path / 'copies'))
    monkeypatch.setattr(config, 'get_deliverables_root', lambda: str(tmp_path / 'outputs'))
    paths = dict(custody.fixed_roots(tmp_path / 'data', tmp_path / 'system'))
    assert tmp_path / 'copies' in paths and tmp_path / 'outputs' in paths


@pytest.mark.parametrize('remote,expected', [('user:password@host:path', 'host:path'), ('git@host:path', 'host:path'), ('ssh://user:password@host/path', 'ssh://host/path')])
def test_remote_userinfo_redacted(remote, expected):
    assert custody.remote_url(remote) == expected


def test_clone_producer_records_created_working_folder(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from ouroboros.gateway.router import collect_routes
    from ouroboros import project_sources
    root = tmp_path / 'data'; folder = repo(tmp_path)
    monkeypatch.setattr(project_sources, 'clone_project_repo', lambda *a: (str(folder), '', ''))
    app = Starlette(routes=collect_routes(data_dir=root)); app.state.drive_root = root; app.state.repo_dir = tmp_path / 'system'
    with TestClient(app) as client:
        response = client.post('/api/projects', json={'id': 'alpha', 'git_url': 'https://example.invalid/repo.git'})
        assert response.status_code == 200, response.text
    target = cleanup.cleanup_plan(root, 'alpha', system_repo_dir=tmp_path / 'system')['targets'][0]
    assert target['default_selected'] and any(s.get('source') == 'clone_producer' for s in target['sources'])


def test_unexpected_inspection_exception_finishes_remaining_targets(setup, tmp_path, monkeypatch):
    root, _, record, plan, close, execute = setup
    path = record(tmp_path / 'target'); path.mkdir(); close(); tokens = confirmed(plan())
    original = cleanup._risk; calls = 0
    def fail_later(*a, **k):
        nonlocal calls
        if len(a) < 3 or a[2] is None:
            calls += 1
        if calls == 2: raise RuntimeError('fresh inspection failed')
        return original(*a, **k)
    # Call executor directly: initial inspect + locked cache lookup + fresh inspect.
    monkeypatch.setattr(cleanup, '_risk', fail_later)
    result = cleanup.cleanup_execute(root, 'alpha', 'complete', list(tokens), tokens, 'agent', tmp_path / 'system')
    assert result['results'][0]['status'] == 'failed' and path.exists()
    assert r.get_reserved_project(root, 'alpha')['cleanup_history'][-1]['phase'] == 'finished'


def test_history_publication_error_stops_effects_and_records_finish(setup, tmp_path, monkeypatch):
    root, system, record, plan, close, _ = setup
    paths = [record(tmp_path / name) for name in ('one', 'two')]
    for path in paths: path.mkdir()
    close(); tokens = confirmed(plan()); original = r._save; calls = 0
    def fail_one(root, data):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError('transient history failure')
        return original(root, data)
    monkeypatch.setattr(r, '_save', fail_one)
    result = cleanup.cleanup_execute(root, 'alpha', 'complete', list(tokens), tokens, 'agent', system)
    assert len(result['results']) == 2 and result['error']
    assert sum(path.exists() for path in paths) == 1
    assert r.get_reserved_project(root, 'alpha')['cleanup_history'][-1]['phase'] == 'finished'


def test_deferred_request_resumes_after_lease_dies(setup, monkeypatch):
    root, system, *_ = setup; save_pending(root, system, cleanup_lease=custody.lease('busy'))
    from supervisor import queue_transitions as transitions
    monkeypatch.setattr(transitions, '_broadcast_projects_changed', lambda *a: None)
    custody.run_pending_cleanup(root, 'alpha')
    resumed = []
    def start(root, pid, chat):
        resumed.append(pid); custody.run_pending_cleanup(root, pid); return True
    monkeypatch.setattr(transitions, 'start_project_deletion', start)
    assert transitions.resume_project_deletions(root) == 0
    monkeypatch.setattr(custody, 'lease_live', lambda lease: False)
    assert transitions.resume_project_deletions(root) == 1 and resumed == ['alpha']
    assert r.get_reserved_project(root, 'alpha')['cleanup_request']['status'] == 'finished'
