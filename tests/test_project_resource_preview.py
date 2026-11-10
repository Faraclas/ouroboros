"""Opt-in project observations are not cleanup authority or an execution plan."""
from __future__ import annotations

import pathlib
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ouroboros import project_management as m, projects_registry as r
from ouroboros.gateway.router import collect_routes


def client(data, repo):
    app = Starlette(routes=collect_routes(data_dir=data))
    app.state.drive_root = data; app.state.repo_dir = repo
    return TestClient(app)


def test_real_http_opt_in_snapshot_alias_overlap_and_purity(tmp_path, monkeypatch):
    data, core, folder = tmp_path / 'data', tmp_path / 'core', tmp_path / 'ordinary'
    core.mkdir(); folder.mkdir(); (folder / 'child').mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(folder, target_is_directory=True)
    protected_alias = tmp_path / 'core-alias'; protected_alias.symlink_to(core, target_is_directory=True)
    subprocess.run(['git', 'init', '-q', str(folder)], check=True)
    # Even repository configuration must not launch its monitor during these rev-parse probes.
    sentinel = tmp_path / 'monitor-ran'
    hook = tmp_path / 'monitor'; hook.write_text(f'#!/bin/sh\ntouch "{sentinel}"\n', encoding='utf-8'); hook.chmod(0o755)
    subprocess.run(['git', '-C', str(folder), 'config', 'core.fsmonitor', str(hook)], check=True)
    r.create_project(data, 'alpha', working_dir=str(folder))
    r.create_project(data, 'beta', working_dir=str(alias / 'child'))
    r.create_project(data, 'closed', working_dir=str(alias))
    r.update_project(data, 'closed', lifecycle='tombstoned')
    r.create_project(data, 'viper-interface', working_dir=str(core))
    m.add_project_resource(data, 'alpha', str(protected_alias))
    m.add_project_resource(data, 'alpha', str(tmp_path / 'absent'))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    # The preview must not enter any preservation or cleanup owner.
    monkeypatch.setattr('ouroboros.body_candidate.retention_verdict', lambda *a, **k: pytest.fail('retention has effects'))
    with client(data, core) as http:
        ordinary = http.get('/api/projects/alpha/detail').json()
        assert set(ordinary) == {'project'}
        for _ in range(2):
            response = http.get('/api/projects/alpha/detail?resource_preview=1')
            assert response.status_code == 200
            body = response.json(); preview = body['resource_preview']
            assert body['project'] == ordinary['project']
            targets = {v['path']: v for v in preview['targets']}
            assert targets[str(folder)]['identity_status'] == 'available'
            refs = targets[str(folder)]['references']
            assert any(v['project_id'] == 'closed' and v['relation'] == 'same' and v['identity_verified'] for v in refs)
            assert any(v['project_id'] == 'beta' and v['relation'] == 'contains' for v in refs)
            assert targets[str(protected_alias)]['runtime_overlap'] is True
            assert targets[str(tmp_path / 'absent')]['identity_status'] == 'missing'
            assert preview['gaps'] and 'No directory contents' in preview['coverage']
            assert 'eligible' not in preview and 'safe' not in preview
    after = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before == after and not sentinel.exists()


def test_paths_missing_alias_permissions_hardlinks_and_case(tmp_path, monkeypatch):
    a = tmp_path / 'A'; a.mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(a, target_is_directory=True)
    first = m._preview_identity(str(a / 'missing'))
    second = m._preview_identity(str(alias / 'missing'))
    assert first['identity_status'] == 'missing' and m._preview_overlap(first, second) == 'same'
    assert m._preview_overlap(m._preview_identity(str(a)), first) == 'contains'
    lower = tmp_path / 'a'; lower.mkdir()
    assert m._preview_overlap(m._preview_identity(str(a)), m._preview_identity(str(lower))) == ''
    file = tmp_path / 'file'; file.write_text('unique', encoding='utf-8')
    link = tmp_path / 'hardlink'; link.hardlink_to(file)
    assert m._preview_overlap(m._preview_identity(str(file)), m._preview_identity(str(link))) == 'same'
    real = pathlib.Path.resolve
    def denied(self, *args, **kwargs):
        if self == a: raise PermissionError('denied')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'resolve', denied)
    assert m._preview_identity(str(a))['identity_status'] == 'unavailable'


def test_empty_and_malformed_neighbours_do_not_invent_complete_coverage(tmp_path, monkeypatch):
    data = tmp_path / 'data'; r.create_project(data, 'empty')
    with client(data, tmp_path / 'core') as http:
        response = http.get('/api/projects/empty/detail?resource_preview=1').json()['resource_preview']
        assert response['targets'] == []
    projects = [{'id': 'bad', 'name': 'Broken', 'resources': 'broken'}]
    result = m._resource_preview(projects, {'resources': [], 'working_dir': ''}, data, None)
    assert len(result['gaps']) == 1 and 'malformed' in result['gaps'][0]


def test_runtime_parent_child_and_shared_habitat_references(tmp_path):
    data, core, habitat = tmp_path / 'data', tmp_path / 'core', tmp_path / 'habitat'
    for path in (core, habitat, habitat / 'nested'): path.mkdir()
    r.create_project(data, 'viper-discord', working_dir=str(habitat))
    r.create_project(data, 'external', working_dir=str(habitat / 'nested'))
    m.add_project_resource(data, 'external', str(tmp_path))
    preview = m.project_detail_view(data, 'external', resource_preview=True, system_repo_dir=core)['resource_preview']
    nested = preview['targets'][0]
    assert any(v['project_id'] == 'viper-discord' and v['relation'] == 'inside' for v in nested['references'])
    assert preview['targets'][1]['runtime_overlap'] is True
    assert not any(key in preview for key in ('removable', 'backup', 'size_bytes', 'active_tasks'))


def test_invalid_neighbour_default_is_gap_not_whole_preview_failure(tmp_path):
    good = tmp_path / 'good'; good.mkdir()
    resources = [{'id': 'r', 'path': str(good), 'label': '', 'added_at': ''}]
    result = m._resource_preview([
        {'id': 'bad', 'name': 'Broken', 'resources': resources, 'working_dir': {'wrong': 'shape'}},
        {'id': 'legacy', 'name': 'Broken legacy', 'working_dir': ['wrong']},
    ], {'resources': resources, 'working_dir': ''}, tmp_path / 'data', None)
    assert result['gaps'] and any('starting folder' in gap for gap in result['gaps'])
    assert result['targets'][0]['references'][0]['project_id'] == 'bad'
