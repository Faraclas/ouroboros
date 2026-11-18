"""Native path identity survives footprint, cleanup transport and selected effects.

All filesystem effects are disposable; POSIX byte names are not Windows names.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ouroboros import projects_registry as r, project_footprint as f
from ouroboros.gateway.router import collect_routes
from ouroboros.task_results import write_task_result
from ouroboros.repository_risk import inspect_repository
from tests.test_repository_risk import repo, git
from tests.test_project_resource_context_tools import world

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX undecodable byte filenames')


def native_targets(parent):
    paths = [parent / os.fsdecode(name) for name in
             (b'raw_\xff', b'raw_\xfe', 'raw_ÿ'.encode(), b'raw_\\udcff', 'raw_�'.encode())]
    for path in paths:
        path.mkdir(); (path / 'keep').write_bytes(b'exact native material')
    return paths


def test_generated_ids_and_real_http_preserve_native_artifact_names(tmp_path):
    root = tmp_path / 'data'; r.create_project(root, 'alpha')
    paths = native_targets(tmp_path)
    write_task_result(root, 'writer', 'completed', project_id='alpha',
                      artifacts=[{'path': str(p)} for p in paths])
    before = r._registry_path(root).read_bytes()
    generated = f.generated_targets(root, 'alpha')['targets']
    assert len({t['id'] for t in generated}) == len(paths)
    for t, path in zip(generated, paths):
        assert os.fsencode(t['path']) == os.fsencode(path)
        if not any(0xD800 <= ord(c) <= 0xDFFF for c in str(path)):
            old = '\0'.join((str(path), 'task.artifacts', 'writer')).encode('utf-8')
            assert t['id'] == 'generated:' + hashlib.sha256(old).hexdigest()
    app = Starlette(routes=collect_routes(data_dir=root))
    app.state.drive_root = root; app.state.repo_dir = Path(__file__).parents[1]
    with TestClient(app) as client:
        detail = client.get('/api/projects/alpha/detail?resource_preview=1')
        assert detail.status_code == 200, detail.text
        targets = detail.json()['resource_preview']['footprint']['generated']['targets']
        assert {os.fsencode(t['path']) for t in targets} == {os.fsencode(p) for p in paths}
        response = client.get('/api/projects/alpha/cleanup/plan')
        assert response.status_code == 200, response.text
        response.content.decode('utf-8')
        assert {os.fsencode(t['path']) for t in response.json()['targets']} == {os.fsencode(p) for p in paths}
    assert r._registry_path(root).read_bytes() == before
    assert all((p / 'keep').read_bytes() == b'exact native material' for p in paths)


def test_agent_confirmations_execute_exact_native_operand_and_history(world, tmp_path):
    data, repos, ctx, tools = world
    def cleanup(**arguments):
        result = tools.execute_result('project_cleanup', {'project_id': 'room', **arguments})
        assert result.status == 'ok', result.text
        return result.text
    paths = native_targets(repos[0].parent)
    for path in paths:
        f.record_workspace_creation(data, 'room', str(path), 'fixture-base')
    footprint = tools.execute_result('project_resources', {'action': 'footprint', 'project_id': 'room'}).text
    footprint.encode('utf-8')
    assert all(str(p) in {e['path'] for e in json.loads(footprint)['footprint']['entries']} for p in paths)
    raw = cleanup(); raw.encode('utf-8')
    plan = json.loads(raw); by_path = {t['path']: t for t in plan['targets']}
    target = by_path[str(paths[0])]; sibling = by_path[str(paths[-1])]
    r.begin_project_deletion(data, 'room'); r.complete_project_deletion(data, 'room')
    # A different filename's token must never confirm this target.
    refused = json.loads(cleanup(action='execute', selections=[target['id']],
        paths=[target['path']], confirmations={target['id']: sibling['warning']['token']}))
    assert next(t for t in refused['results'] if t['id'] == target['id'])['status'] == 'refused'
    current = next(t for t in json.loads(cleanup())['targets'] if t['id'] == target['id'])
    result = cleanup(action='execute', selections=[current['id']],
        paths=[current['path']], confirmations={current['id']: current['warning']['token']})
    result.encode('utf-8')
    assert next(t for t in json.loads(result)['results'] if t['id'] == current['id'])['status'] == 'removed'
    assert not paths[0].exists() and all((p / 'keep').exists() for p in paths[1:])
    # Read the durable registry again, not a held result: recovery retains bytes.
    history = r._load(data, strict=True)['projects'][0]['cleanup_history']
    assert any(h.get('path') == str(paths[0]) and h.get('status') == 'removed' for h in history)


def test_native_git_identity_and_external_link_dependency(tmp_path):
    from ouroboros.project_cleanup import _git
    from ouroboros.project_cleanup_context import repository_dependency
    original = repo(tmp_path)
    parent = tmp_path / 'nested'; parent.mkdir()
    path = parent / os.fsdecode(b' repo_\xff\n ')
    original.rename(path)
    linked = tmp_path / os.fsdecode(b' linked_\xfe\n ')
    git(path, 'worktree', 'add', '--detach', str(linked))
    observed = inspect_repository(str(linked))
    assert observed['status'] == 'repository', observed
    assert observed['arrangement'] == 'linked'
    assert os.fsencode(observed['facts']['root']) == os.fsencode(linked)
    assert os.fsencode(observed['facts']['common_dir']) == os.fsencode(path / '.git')
    assert {os.fsencode(p) for p in observed['facts']['worktrees']} == {os.fsencode(path), os.fsencode(linked)}
    enclosing = inspect_repository(str(path))
    assert repository_dependency(path, enclosing, _git) == 'other_git_worktrees_depend_on_repository'
    assert repository_dependency(parent, {'status': 'not_repository'}, _git) == 'other_git_worktrees_depend_on_repository'
    from ouroboros.project_cleanup import cleanup_plan, cleanup_execute
    root = tmp_path / 'data'; r.create_project(root, 'alpha')
    f.record_workspace_creation(root, 'alpha', str(linked), 'fixture-base')
    r.begin_project_deletion(root, 'alpha'); r.complete_project_deletion(root, 'alpha')
    target = cleanup_plan(root, 'alpha', system_repo_dir=tmp_path / 'system')['targets'][0]
    result = cleanup_execute(root, 'alpha', 'complete', [target['id']],
        {target['id']: target['warning']['token']}, 'agent', tmp_path / 'system')
    assert result['results'][0]['status'] == 'removed', result
    assert not linked.exists() and (path / 'file').exists()
    assert inspect_repository(str(path))['facts']['worktrees'] == [str(path)]


def test_mount_inventory_preserves_native_bytes(tmp_path, monkeypatch):
    from ouroboros.project_cleanup_context import mount_paths, contains_mount
    table = tmp_path / 'mountinfo'
    table.write_bytes(b'1 2 0:1 / /native_\xff\\040mount rw - tmpfs tmpfs rw\n')
    read = Path.read_text
    def read_inventory(path, **kwargs):
        return read(table if str(path) == '/proc/self/mountinfo' else path, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_inventory)
    mounts = mount_paths()
    assert [os.fsencode(p) for p in mounts] == [b'/native_\xff mount']
    assert contains_mount(os.fsdecode(b'/native_\xff mount'), mounts)
    assert not contains_mount('/native_ÿ mount', mounts)
