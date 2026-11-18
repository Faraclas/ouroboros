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
