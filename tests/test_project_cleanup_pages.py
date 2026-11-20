"""Bounded observations preserve the complete execution guard universe."""
import json
from pathlib import Path

import pytest
from ouroboros import project_cleanup as c, project_footprint as f
from ouroboros.project_management import ProjectManagementError
from tests.test_project_cleanup import setup


def test_pages_are_complete_native_and_digest_bound(setup, tmp_path, monkeypatch):
    root, system, _, _, _, _ = setup
    paths = [root / 'artifacts' / str(i) for i in range(57)]
    paths[30] = root / 'artifacts' / 'native-\udcff'
    for path in paths: path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(p), 'source': 'task.artifacts', 'origin': 'recorded_output'} for p in paths], 'gaps': []})
    full = c.cleanup_plan(root, 'alpha', system_repo_dir=system)
    digest = ''; offset = 0; rows = []
    while True:
        page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, offset=offset, limit=7, plan_sha256=digest)
        assert page['total'] == page['matched_total'] == page['retained_total'] == 57
        assert len(page['targets']) <= 7 and not page['immutable']
        digest = page['plan_sha256']; rows.extend(page['targets'])
        if page['next_offset'] is None: break
        offset = page['next_offset']
    assert rows == full['targets']
    assert len({t['id'] for t in rows}) == 57
    native = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, query='native-', plan_sha256=digest)
    assert native['matched_total'] == 1 and native['targets'][0]['path'] == str(paths[30])
    assert json.loads(json.dumps(native))['targets'][0]['path'] == str(paths[30])
    paths[0].rmdir()
    with pytest.raises(ProjectManagementError, match='changed'):
        c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, offset=7, plan_sha256=digest)


@pytest.mark.parametrize('kwargs', [{'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 101}, {'query': 'x' * 2001}])
def test_invalid_paging_refuses_before_planning(monkeypatch, kwargs):
    monkeypatch.setattr(c, 'cleanup_plan', lambda *a, **k: pytest.fail('must validate first'))
    with pytest.raises(ProjectManagementError): c.cleanup_plan_page(None, 'alpha', **kwargs)


def test_page_never_limits_retained_child_guard(setup, tmp_path):
    root, system, record, _, close, _ = setup
    parent = record(tmp_path / 'parent'); parent.mkdir()
    child = record(parent / 'keep'); child.mkdir(); (child / 'unique').write_text('keep')
    close()
    page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=1)
    assert page['total'] == 2 and len(page['targets']) == 1
    target = page['targets'][0]
    result = c.cleanup_execute(root, 'alpha', 'complete', [target['id']],
        {target['id']: target['warning']['token']}, 'agent', system)
    assert result['results'][0]['reason'] == 'contains_retained_target'
    assert result['unselected_retained_count'] == 1
    assert (child / 'unique').read_text() == 'keep'


def test_existing_recorded_scale_no_ancestor_walk_or_loss_probe(setup, monkeypatch):
    root, system, _, _, _, _ = setup
    # Same order of magnitude as the actual failing observation; every target exists.
    paths = [root / 'artifacts' / str(i) for i in range(27555)]
    for path in paths: path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(p), 'source': 'task.artifacts', 'origin': 'recorded_output'} for p in paths], 'gaps': []})
    monkeypatch.setattr(c.custody, 'context', lambda *a: {
        'copies': [], 'task_ids': set(), 'processes': False, 'workspaces': [],
        'process_cwds': [str(root / 'artifacts' / str(i) / 'nested') for i in range(12)]})
    monkeypatch.setattr(c, 'inspect_repository', lambda *a: pytest.fail('protected material must stay loss-uninspected'))
    original = Path.is_relative_to
    calls = []
    def counted(self, other):
        calls.append((self, other)); return original(self, other)
    monkeypatch.setattr(Path, 'is_relative_to', counted)
    page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system)
    assert page['total'] == 27555 and len(page['targets']) == 25
    assert all(t['exists'] and t['action'] == 'retain' for t in page['targets'])
    assert len(json.dumps(page)) < 100000
    assert len(calls) < 100, 'comparison work must not walk ancestors per target/cwd'


def test_page_risk_is_local_and_execution_history_is_selected_only(setup, tmp_path, monkeypatch):
    root, system, _, _, close, _ = setup
    paths = [tmp_path / 'material' / str(i) for i in range(300)]
    for path in paths: path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(p), 'source': 'create_producer', 'origin': 'project_created'} for p in paths], 'gaps': []})
    inspected = []; inspect = c.inspect_repository
    def counted(path):
        inspected.append(path); return inspect(path)
    monkeypatch.setattr(c, 'inspect_repository', counted)
    close()
    page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2)
    assert inspected == [str(paths[0]), str(paths[1])]
    target = page['targets'][0]; digest = page['plan_sha256']
    # Unseen Git/loss facts are not a pagination lease; effect tokens are checked afresh.
    (paths[-1] / 'new-local-work').write_text('must stay')
    inspected.clear()
    c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2, plan_sha256=digest)
    assert inspected == [str(paths[0]), str(paths[1])]
    inspected.clear()
    result = c.cleanup_execute(root, 'alpha', 'complete', [target['id']],
        {target['id']: target['warning']['token']}, 'agent', system)
    assert set(inspected) == {str(paths[0])}
    assert len(result['results']) == 1 and result['results'][0]['status'] == 'removed'
    assert result['unselected_retained_count'] == 299
    from ouroboros import projects_registry as r
    history = r.get_reserved_project(root, 'alpha')['cleanup_history']
    assert [row['phase'] for row in history] == ['started', 'target', 'finished']
    assert len(history[-1]['results']) == 1
    assert (paths[-1] / 'new-local-work').read_text() == 'must stay'


def test_absent_targets_not_default_selected_and_bulk_eligible_across_pages(setup, tmp_path, monkeypatch):
    root, system, record, _, close, _ = setup
    # Mix: 2 protected artifacts, 2 already-gone (1 created, 1 task_associated),
    # 1 attached_external needing confirmation, 3 clean empty created dirs across 2 pages (limit=2),
    # and 1 off-page created dir with unique work.
    art1 = tmp_path / 'art1'; art1.mkdir()
    art2 = tmp_path / 'art2'; art2.mkdir()
    gone_created = tmp_path / 'gone-created'
    gone_task = tmp_path / 'gone-task'
    ext = record(tmp_path / 'ext', origin='unknown'); ext.mkdir(); (ext / 'work').write_text('keep')
    import subprocess
    def init_clean(path):
        path.mkdir(parents=True, exist_ok=True)
        remote = tmp_path / f'{path.name}-remote.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.email', 't@example.com'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'Test'], check=True, capture_output=True)
        (path / 'README').write_text('ok')
        subprocess.run(['git', '-C', str(path), 'add', 'README'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'commit', '-m', 'init'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'push', '-u', 'origin', 'HEAD'], check=True, capture_output=True)
    clean = [tmp_path / f'clean-{i}' for i in range(3)]
    for p in clean: init_clean(p)
    unique_offpage = tmp_path / 'z-unique-offpage'; unique_offpage.mkdir(); (unique_offpage / 'file').write_text('unconfirmed')
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(art1), 'source': 'task.artifacts', 'origin': 'recorded_output'},
        {'path': str(art2), 'source': 'task.artifacts', 'origin': 'recorded_output'},
        {'path': str(gone_created), 'source': 'create_producer', 'origin': 'project_created'},
        {'path': str(gone_task), 'source': 'task.workspace_root', 'origin': 'unknown'},
        {'path': str(clean[0]), 'source': 'create_producer', 'origin': 'project_created'},
        {'path': str(clean[1]), 'source': 'create_producer', 'origin': 'project_created'},
        {'path': str(clean[2]), 'source': 'private_copy_record', 'origin': 'body_candidate'},
        {'path': str(unique_offpage), 'source': 'create_producer', 'origin': 'project_created'},
    ], 'gaps': []})
    inspected = []; inspect = c.inspect_repository
    def counted(path):
        inspected.append(path); return inspect(path)
    monkeypatch.setattr(c, 'inspect_repository', counted)
    page0 = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2)
    assert inspected == [str(ext)]  # only page 0's non-protected target (ext) was probed
    assert page0['total'] == 9
    assert page0['protected_total'] == 2
    assert page0['already_gone_total'] == 2
    assert page0['targets_sha256'] == ''  # generic caller retains bounded page inspection
    assert page0['needs_confirmation_total'] == 1  # ext
    inspected.clear()
    bulk_page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2, inspect_bulk=True)
    assert len(bulk_page['targets']) == 2  # no bulk target list sent to the browser
    assert {*map(str, clean), str(unique_offpage)} <= set(inspected)
    assert not {str(art1), str(art2), str(gone_created), str(gone_task)} & set(inspected)
    assert bulk_page['eligible_total'] == 4  # 3 clean + 1 unique_offpage; external is never bulk-selected
    assert bulk_page['needs_confirmation_total'] == 2  # external and unique off-page
    assert bulk_page['eligible_needs_confirmation_total'] == 1
    page1 = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, offset=3, limit=2, plan_sha256=page0['plan_sha256'])
    gone_rows = [t for t in page1['targets'] if t['exists'] is False]
    assert len(gone_rows) == 2
    assert all(not t['default_selected'] and not t['eligible'] and t['action'] == 'already_absent' for t in gone_rows)
    close()
    # Re-read targets_sha256 after close (lifecycle changed, targets_sha256 is current)
    closed_page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2, inspect_bulk=True)
    assert bulk_page['targets_sha256'] == closed_page['targets_sha256']  # safe Close lifecycle change alone
    novel = clean[2] / 'new-work'
    novel.write_text('work added after preview')
    with pytest.raises(ProjectManagementError, match='changed'):
        c.cleanup_execute(root, 'alpha', 'complete', [], {}, 'human', system,
                          select_all_eligible=True, targets_sha256=closed_page['targets_sha256'])
    assert all(p.exists() for p in clean) and novel.exists()
    novel.unlink()
    closed_page = c.cleanup_plan_page(root, 'alpha', system_repo_dir=system, limit=2, inspect_bulk=True)
    with pytest.raises(ProjectManagementError, match='targets_sha256'):
        c.cleanup_execute(root, 'alpha', 'complete', [], {}, 'human', system, select_all_eligible=True)
    # Exclude clean[1], bulk-select the rest across unseen pages:
    # clean[0] and clean[2] are empty (no warning) -> removed;
    # unique_offpage has local file (warning) and no confirmation -> refused!
    # ext, protected, and gone are untouched.
    clean1_id = 'cleanup:' + c._hash(str(clean[1]))
    res = c.cleanup_execute(
        root, 'alpha', 'complete', [], {}, 'human', system,
        select_all_eligible=True, excluded_ids=[clean1_id], targets_sha256=closed_page['targets_sha256'],
    )
    by_path = {r['path']: r for r in res['results']}
    assert by_path[str(clean[0])]['status'] == 'removed' and not clean[0].exists()
    assert by_path[str(clean[2])]['status'] == 'removed' and not clean[2].exists()
    assert str(clean[1]) not in by_path and clean[1].exists()
    assert by_path[str(unique_offpage)]['status'] == 'refused'
    assert by_path[str(unique_offpage)]['reason'] == 'current_warning_confirmation_required'
    assert (unique_offpage / 'file').read_text() == 'unconfirmed'
    assert str(ext) not in by_path and (ext / 'work').read_text() == 'keep'
    # Stale targets_sha256 refuses rather than silently adding newly eligible targets.
    new_dir = tmp_path / 'newly-added'; new_dir.mkdir()
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(new_dir), 'source': 'create_producer', 'origin': 'project_created'},
    ], 'gaps': []})
    with pytest.raises(ProjectManagementError, match='changed'):
        c.cleanup_execute(
            root, 'alpha', 'complete', [], {}, 'human', system,
            select_all_eligible=True, targets_sha256=closed_page['targets_sha256'],
        )
    assert new_dir.exists()
