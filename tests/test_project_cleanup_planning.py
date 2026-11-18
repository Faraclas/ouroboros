"""Planning remains useful with retained or caller-inaccessible recorded targets."""
from collections import Counter
from pathlib import Path

from ouroboros import project_cleanup as cleanup, project_footprint as f
from tests.test_project_cleanup import setup


def test_denied_target_and_alias_protect_parent_without_loss_inspection(setup, tmp_path, monkeypatch):
    root, system, record, plan, close, _ = setup
    parent = record(tmp_path / 'parent'); parent.mkdir()
    child = parent / 'private'; child.mkdir(); (child / 'keep').write_text('unique')
    alias = record(tmp_path / 'alias'); alias.symlink_to(child, target_is_directory=True)
    other = record(tmp_path / 'other'); other.mkdir()
    def access(path):
        return 'physical denial' if path == str(alias) else ''
    inspect = cleanup.inspect_repository
    inspected = []
    def checked(path):
        assert path != str(alias)
        inspected.append(path)
        return inspect(path)
    monkeypatch.setattr(cleanup, 'inspect_repository', checked)
    original_identity = cleanup._preview_identity
    def identity(path):
        assert path != str(alias), 'denied target must not be statted by planning'
        return original_identity(path)
    monkeypatch.setattr(cleanup, '_preview_identity', identity)
    close()
    result = cleanup.cleanup_plan(root, 'alpha', system_repo_dir=system, authorize=access)
    targets = {t['path']: t for t in result['targets']}
    denied = targets[str(alias)]
    assert denied['retain_reason'] == 'caller_access_denied' and denied['exists'] is None
    assert denied['risk']['status'] == 'not_inspected' and not denied['risk']['facts']
    assert denied['access_denial'] == 'physical denial' and not denied['default_selected']
    assert denied['identity']['resolved_path'] == str(child)
    assert result['gaps'] and len(targets) == 3
    tokens = {t['id']: (t['warning'] or {}).get('token', t['selection_token']) for t in targets.values()}
    outcome = cleanup.cleanup_execute(root, 'alpha', 'complete', list(tokens), tokens, 'agent', system,
                                      read_authorize=access, authorize=access)
    rows = {r['path']: r for r in outcome['results']}
    assert rows[str(alias)]['status'] == 'refused'
    assert rows[str(parent)]['reason'] == 'contains_retained_target'
    assert rows[str(other)]['status'] == 'removed' and not other.exists()
    assert (child / 'keep').read_text() == 'unique' and alias.is_symlink()


def test_retained_generated_scale_reuses_identities_and_never_probes_loss(setup, tmp_path, monkeypatch):
    root, system, _, _, _, _ = setup
    paths = [tmp_path / 'outputs' / str(i) for i in range(120)]
    copies = [{'path': str(tmp_path / 'copies' / str(i)), 'task_id': 'other'} for i in range(30)]
    monkeypatch.setattr(cleanup.custody, 'context', lambda *a: {
        'copies': copies, 'task_ids': set(), 'processes': False, 'workspaces': [], 'process_cwds': []})
    monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
        {'path': str(p), 'source': 'task.artifacts', 'origin': 'recorded_output'} for p in paths], 'gaps': []})
    def no_loss_probe(*a, **k):
        raise AssertionError('retained artifacts must not run Git or nested-content inspection')
    monkeypatch.setattr(cleanup, 'inspect_repository', no_loss_probe)
    calls = Counter(); identity = cleanup._preview_identity
    def observed(path):
        calls[path] += 1
        return identity(path)
    monkeypatch.setattr(cleanup, '_preview_identity', observed)
    result = cleanup.cleanup_plan(root, 'alpha', system_repo_dir=system)
    assert len(result['targets']) == len(paths)
    assert all(t['action'] == 'retain' and t['risk']['status'] == 'not_inspected' for t in result['targets'])
    assert max(calls.values()) == 1


def test_protection_ending_requires_fresh_loss_confirmation(setup, tmp_path, monkeypatch):
    root, system, record, plan, close, _ = setup
    path = record(tmp_path / 'target'); path.mkdir()
    context = {'copies': [], 'task_ids': set(), 'processes': True, 'workspaces': [], 'process_cwds': []}
    monkeypatch.setattr(cleanup.custody, 'context', lambda *a: context)
    old = plan()['targets'][0]
    assert old['retain_reason'] and old['risk']['status'] == 'not_inspected'
    context['processes'] = False; close()
    result = cleanup.cleanup_execute(root, 'alpha', 'complete', [old['id']],
                                     {old['id']: old['selection_token']}, 'agent', system)
    assert result['results'][0]['status'] == 'refused' and path.exists()
    fresh = plan()['targets'][0]
    assert fresh['warning'] and not fresh['retain_reason']
    assert cleanup.cleanup_execute(root, 'alpha', 'complete', [fresh['id']],
        {fresh['id']: fresh['warning']['token']}, 'agent', system)['results'][0]['status'] == 'removed'


def test_parent_alias_denial_survives_grouping_in_both_orders(setup, tmp_path, monkeypatch):
    root, system, _, _, close, _ = setup
    real = tmp_path / 'real'; real.mkdir(); target = real / 'child'; target.mkdir()
    (target / 'keep').write_text('unique')
    alias = tmp_path / 'alias'; alias.symlink_to(real, target_is_directory=True)
    spellings = [str(target), str(alias / 'child')]
    def access(path): return 'denied alias reference' if path == spellings[1] else ''
    def unexpected(*a): raise AssertionError('group containing a denied reference must not inspect loss')
    monkeypatch.setattr(cleanup, 'inspect_repository', unexpected)
    close()
    for order in (spellings, list(reversed(spellings))):
        monkeypatch.setattr(f, 'generated_targets', lambda *a: {'targets': [
            {'path': p, 'source': 'private_copy_record', 'origin': 'body_candidate'} for p in order], 'gaps': []})
        plan = cleanup.cleanup_plan(root, 'alpha', system_repo_dir=system, authorize=access)
        assert len(plan['targets']) == 1
        row = plan['targets'][0]
        assert row['access_denial'] == 'denied alias reference' and row['risk']['status'] == 'not_inspected'
        assert {s['path'] for s in row['sources']} == set(spellings)
        result = cleanup.cleanup_execute(root, 'alpha', 'complete', [row['id']],
            {row['id']: row['selection_token']}, 'agent', system, read_authorize=access, authorize=access)
        assert result['results'][0]['reason'] == 'caller_access_denied'
        assert (target / 'keep').read_text() == 'unique'
