"""Root-only, Safety-checked cleanup uses normal physical binding authority."""
import json
from ouroboros import projects_registry as r
from ouroboros.tools.project_cleanup import _project_cleanup, cleanup_tool_entries
from tests.test_project_resource_context_tools import world  # shared disposable tool-world fixture


def test_root_plan_execute_requires_extra_confirmation(world):
    data, repos, ctx, tools = world
    entry = tools._entries['project_cleanup']
    assert entry.safety_policy == 'check'
    plan = json.loads(_project_cleanup(ctx, 'room'))
    target = plan['targets'][0]
    assert target['warning'] and not target['default_selected']
    r.begin_project_deletion(data, 'room'); r.complete_project_deletion(data, 'room')
    result = json.loads(_project_cleanup(ctx, 'room', 'execute', selections=[target['id']], paths=[target['path']], confirmations={}))
    assert result['results'][0]['status'] == 'refused' and repos[0].exists()
    result = json.loads(_project_cleanup(ctx, 'room', 'execute', selections=[target['id']], paths=[target['path']], confirmations={target['id']: target['warning']['token']}))
    assert result['results'][0]['status'] == 'removed', result
    assert not repos[0].exists() and repos[1].exists()
    assert r.get_reserved_project(data, 'room')['cleanup_history'][-1]['decided_by'] == 'agent-confirmed'


def test_child_and_physical_denial_do_not_inspect_or_remove(world, monkeypatch):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools import project_cleanup as tool
    _, repos, ctx, _ = world
    monkeypatch.setattr(tool, '_access', lambda *_: 'physical denial')
    denied = json.loads(_project_cleanup(ctx, 'room'))
    assert denied['targets'] and all(t['access_denial'] == 'physical denial' for t in denied['targets'])
    assert all(t['retain_reason'] == 'caller_access_denied' and t['exists'] is None for t in denied['targets'])
    ctx.is_direct_chat = False; ctx.task_metadata = {'parent_task_id': 'parent', 'delegation_role': 'subagent'}
    ctx.task_constraint = TaskConstraint(mode='acting_subagent', surface='self_worktree', write_root=str(repos[0]))
    assert 'ordinary root' in _project_cleanup(ctx, 'room')
    assert repos[0].exists()


def test_execute_cannot_hide_actual_paths_from_safety(world):
    data, repos, ctx, tools = world
    target = json.loads(_project_cleanup(ctx, 'room'))['targets'][0]
    r.begin_project_deletion(data, 'room'); r.complete_project_deletion(data, 'room')
    reply = _project_cleanup(ctx, 'room', 'execute', selections=[target['id']],
                             confirmations={target['id']: target['warning']['token']})
    assert 'requires paths' in reply and repos[0].exists()
    for paths in ([], ['/pretend/safe']):
        reply = _project_cleanup(ctx, 'room', 'execute', selections=[target['id']],
                                 confirmations={target['id']: target['warning']['token']}, paths=paths)
        assert 'requires paths' in reply and repos[0].exists()
    entry = cleanup_tool_entries()[0]
    assert 'paths' in entry.schema['parameters']['properties']


def _setup_copy_world(world, tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from ouroboros import body_candidate, subagent_worktrees as wt
    from ouroboros.task_results import write_task_result
    data, repos, ctx, tools = world
    wt_root = tmp_path / 'subagent_worktrees'
    proj_root = tmp_path / 'subagent_projects'
    proj_root.mkdir()
    monkeypatch.setenv('OUROBOROS_SUBAGENT_WORKTREE_ROOT', str(wt_root))
    monkeypatch.setenv('OUROBOROS_SUBAGENT_PROJECTS_ROOT', str(proj_root))
    monkeypatch.setattr('supervisor.queue.task_settlement_liveness', lambda _tid: False)
    ext_repo = proj_root / 'genesis-app'
    ext_repo.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(ext_repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(ext_repo), 'config', 'user.email', 'test@example.com'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(ext_repo), 'config', 'user.name', 'Tester'], check=True, capture_output=True)
    (ext_repo / 'app.py').write_text('print(1)\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(ext_repo), 'add', 'app.py'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(ext_repo), 'commit', '-qm', 'init'], check=True, capture_output=True)
    sys_repo = tmp_path / 'serving_body'
    sys_repo.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(sys_repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(sys_repo), 'config', 'user.email', 'test@example.com'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(sys_repo), 'config', 'user.name', 'Tester'], check=True, capture_output=True)
    (sys_repo / 'core.py').write_text('x = 1\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(sys_repo), 'add', 'core.py'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(sys_repo), 'commit', '-qm', 'base'], check=True, capture_output=True)
    ctx.repo_dir = sys_repo
    ctx.serving_repo_dir = sys_repo
    r.create_project(data, 'closed-copies', name='Closed Copies')
    for tid in ('child-copy', 'del-git', 'del-payload', 'cand-task'):
        write_task_result(data, tid, 'completed', project_id='closed-copies')
    # Provision child-copy from uncommitted/untracked ext_repo state so refs/heads/subagent/child-copy
    # holds a distinct commit not reachable from main HEAD (commits_not_in_remote_tracking drops on retirement).
    (ext_repo / 'untracked_wip.py').write_text('wip = True\n', encoding='utf-8')
    child_wt = wt.provision_worktree(
        repo_dir=ext_repo, task_id='child-copy', worktree_root=wt_root, data_dir=data, source_is_system_repo=False)
    (ext_repo / 'untracked_wip.py').unlink()
    snap_git = wt.provision_execution_snapshot(
        target_root=ext_repo, task_id='del-git', snapshot_id='snap-git-1', worktree_root=wt_root, data_dir=data)
    (Path(snap_git.path) / 'dirty.txt').write_text('uncommitted change\n', encoding='utf-8')
    payload_src = tmp_path / 'skill_payload_src'
    payload_src.mkdir()
    (payload_src / 'SKILL.md').write_text('# skill\n', encoding='utf-8')
    snap_payload = wt.provision_payload_snapshot(
        target_root=payload_src, task_id='del-payload', snapshot_id='snap-pay-1', worktree_root=wt_root, data_dir=data)
    cand = body_candidate._provision(sys_repo, 'cand-task', data)
    other_cand = body_candidate._provision(sys_repo, 'ctx-owner', data)
    # Bind ctx.system_repo_dir to another candidate so serving_repo_dir_for(ctx) != ctx.system_repo_dir.
    ctx.system_repo_dir = Path(other_cand['path'])
    return data, ctx, tools, wt_root, ext_repo, sys_repo, {
        'child_wt': Path(child_wt.path), 'snap_git': Path(snap_git.path),
        'snap_payload': Path(snap_payload.path), 'body_cand': Path(cand['path'])}


def test_agent_cleanup_retires_all_copy_kinds_and_keeps_ordinary_tools_confined(world, tmp_path, monkeypatch):
    from pathlib import Path
    from ouroboros import project_cleanup_context as custody
    data, ctx, tools, wt_root, ext_repo, sys_repo, copies = _setup_copy_world(world, tmp_path, monkeypatch)
    # Verify ordinary file/shell tools stay confined outside worktree_root and subagent_projects.
    read_copy = tools.execute_result('read_file', {'path': str(copies['child_wt'] / 'app.py')})
    assert read_copy.status in {'blocked', 'error'} and 'print(1)' not in read_copy.text
    assert tools.execute_result('write_file', {'path': str(copies['child_wt'] / 'x.txt'), 'content': 'no'}).status in {'blocked', 'error'}
    assert tools.execute_result('edit_text', {'path': str(copies['child_wt'] / 'app.py'), 'old_str': '1', 'new_str': '2'}).status in {'blocked', 'error'}
    assert tools.execute_result('list_files', {'path': str(copies['child_wt'])}).status in {'blocked', 'error'}
    saved_mode = ctx.workspace_mode
    ctx.workspace_mode = ''
    assert tools.execute_result('run_command', {'cmd': ['pwd'], 'cwd': str(copies['child_wt'])}).status in {'blocked', 'error'}
    ctx.workspace_mode = saved_mode
    assert tools.execute_result('read_file', {'path': str(ext_repo / 'app.py')}).status == 'ok'  # subagent_projects is readable
    assert tools.execute_result('write_file', {'path': str(ext_repo / 'x.txt'), 'content': 'no'}).status in {'blocked', 'error'}
    r.begin_project_deletion(data, 'closed-copies'); r.complete_project_deletion(data, 'closed-copies')
    calls = []
    real_records = custody.copy_records
    monkeypatch.setattr(custody, 'copy_records', lambda root: calls.append(root) or real_records(root))
    plan = json.loads(_project_cleanup(ctx, 'closed-copies'))
    # _access memoizes copy_records per call, so plan reads copy_records twice total (once in _access closure, once in custody.context).
    assert len(calls) <= 2
    by_path = {t['path']: t for t in plan['targets']}
    assert set(str(p) for p in copies.values()) <= set(by_path)
    for p in copies.values():
        row = by_path[str(p)]
        assert row['exists'] is True and not row['protection'] and not row['retain_reason']
        assert not row['access_denial'] and row['action'] == 'remove_if_selected'
    dirty_row = by_path[str(copies['snap_git'])]
    assert dirty_row['warning'] and dirty_row['warning']['unique_work']
    # Missing warning confirmation refuses without touching files.
    dirty_paths = [dirty_row['path'], dirty_row['identity']['resolved_path'], dirty_row['risk']['facts']['common_dir']]
    refused = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute', selections=[dirty_row['id']], paths=dirty_paths, confirmations={}))
    assert refused['results'][0]['reason'] == 'current_warning_confirmation_required'
    assert copies['snap_git'].exists()
    # Retire all four copies (external subagent_projects git/child, standalone payload, and body_candidate).
    selected = [by_path[str(p)] for p in copies.values()]
    exec_paths = sorted({
        *{x for t in selected for x in (t['path'], t['identity']['resolved_path'])},
        *{t['risk']['facts']['common_dir'] for t in selected if t['risk'].get('arrangement') == 'linked'},
    })
    tokens = {t['id']: (t['warning']['token'] if t['warning'] else t['selection_token']) for t in selected}
    result = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute',
                                        selections=[t['id'] for t in selected], paths=exec_paths, confirmations=tokens))
    assert all(row['status'] == 'removed' for row in result['results']), result['results']
    assert all(not p.exists() for p in copies.values())
    remaining_records = list((data / 'state/private_copies').glob('*.json'))
    assert len(remaining_records) == 1 and 'ctx-owner' in remaining_records[0].name
    assert (ext_repo / 'app.py').exists() and (sys_repo / 'core.py').exists()


def test_agent_cleanup_copy_boundaries_negative_controls(world, tmp_path, monkeypatch):
    from pathlib import Path
    from ouroboros import body_candidate, subagent_worktrees as wt, unindexed
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.project_management import add_project_resource
    from ouroboros.task_results import write_task_result
    data, ctx, tools, wt_root, ext_repo, sys_repo, copies = _setup_copy_world(world, tmp_path, monkeypatch)
    copies_dir = wt_root / unindexed.COPIES
    outside_target = tmp_path / 'outside_keep'; outside_target.mkdir(); (outside_target / 'secret.txt').write_text('keep')
    escape_link = copies_dir / 'escape_link'; escape_link.symlink_to(outside_target, target_is_directory=True)
    unrecorded = copies_dir / 'unrecorded_dir'; unrecorded.mkdir(); (unrecorded / 'keep.txt').write_text('keep')
    absent_copy = copies_dir / 'retired_absent_copy'
    nested_sub = copies['snap_payload'] / 'nested_cwd'; nested_sub.mkdir(); (nested_sub / 'f.txt').write_text('keep')
    # Live task copy with workspace_root is retained at plan; an unreleased copy without workspace_root is refused by retire_copy.
    live_wt = wt.provision_worktree(repo_dir=ext_repo, task_id='live-copy', worktree_root=wt_root, data_dir=data, source_is_system_repo=False)
    write_task_result(data, 'live-copy', 'running', project_id='closed-copies', workspace_root=str(live_wt.path))
    unreleased_wt = wt.provision_worktree(repo_dir=ext_repo, task_id='unreleased-copy', worktree_root=wt_root, data_dir=data, source_is_system_repo=False)
    write_task_result(data, 'unreleased-copy', 'running', project_id='closed-copies')
    monkeypatch.setattr('supervisor.queue.task_settlement_liveness', lambda tid: tid != 'cand-task' and tid != 'del-git' and tid != 'del-payload' and tid != 'child-copy')
    for bad in (wt_root, copies_dir, escape_link, unrecorded):
        add_project_resource(data, 'closed-copies', str(bad))
    write_task_result(data, 'absent-holder', 'completed', project_id='closed-copies', workspace_root=str(absent_copy))
    write_task_result(data, 'nested-holder', 'completed', project_id='closed-copies', workspace_root=str(nested_sub))
    r.begin_project_deletion(data, 'closed-copies'); r.complete_project_deletion(data, 'closed-copies')
    plan = json.loads(_project_cleanup(ctx, 'closed-copies'))
    by_path = {t['path']: t for t in plan['targets']}
    for bad in (wt_root, copies_dir, escape_link, unrecorded, nested_sub):
        row = by_path[str(bad)]
        assert row['protection'] == 'caller_access_denied' and row['exists'] is None and row['access_denial']
    assert by_path[str(absent_copy)]['exists'] is False and by_path[str(absent_copy)]['action'] == 'already_absent'
    assert by_path[str(Path(live_wt.path))]['retain_reason'] == 'live_task_or_process_uses_target'
    unrel_row = by_path[str(Path(unreleased_wt.path))]
    unrel_tok = unrel_row['warning']['token'] if unrel_row['warning'] else unrel_row['selection_token']
    unrel_paths = [unrel_row['path'], unrel_row['identity']['resolved_path'], unrel_row['risk']['facts']['common_dir']]
    unrel_res = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute',
                                           selections=[unrel_row['id']], paths=unrel_paths,
                                           confirmations={unrel_row['id']: unrel_tok}))
    assert unrel_res['results'][0]['reason'] == 'retirement_owner_retained_copy' and Path(unreleased_wt.path).exists()
    # Nested denied subpath must block parent recorded copy with contains_retained_target.
    parent_row = by_path[str(copies['snap_payload'])]
    parent_paths = [parent_row['path'], parent_row['identity']['resolved_path']]
    parent_tok = parent_row['warning']['token'] if parent_row['warning'] else parent_row['selection_token']
    blocked_parent = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute',
                                                selections=[parent_row['id']], paths=parent_paths,
                                                confirmations={parent_row['id']: parent_tok}))
    assert blocked_parent['results'][0]['reason'] == 'contains_retained_target' and copies['snap_payload'].exists()
    # common_dir mismatch: tamper copy_record.git_dir so it differs from observed common_dir under subagent_projects.
    from ouroboros import subagent_worktrees as sw
    sw._update_record(copies['child_wt'], lambda row: {**row, 'git_dir': str(tmp_path / 'other/.git')}, data_dir=data, op='test')
    child_row = json.loads(_project_cleanup(ctx, 'closed-copies'))['targets']
    child_t = next(t for t in child_row if t['path'] == str(copies['child_wt']))
    child_paths = [child_t['path'], child_t['identity']['resolved_path'], child_t['risk']['facts']['common_dir']]
    child_tok = child_t['warning']['token'] if child_t['warning'] else child_t['selection_token']
    mismatch_res = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute',
                                              selections=[child_t['id']], paths=child_paths,
                                              confirmations={child_t['id']: child_tok}))
    assert mismatch_res['results'][0]['status'] == 'refused' and copies['child_wt'].exists()
    # Light mode blocks serving system_repo common_dir mutation with light_mode_system_repo_blocked.
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'light')
    cand_row = next(t for t in json.loads(_project_cleanup(ctx, 'closed-copies'))['targets'] if t['path'] == str(copies['body_cand']))
    cand_paths = [cand_row['path'], cand_row['identity']['resolved_path'], cand_row['risk']['facts']['common_dir']]
    cand_tok = cand_row['warning']['token'] if cand_row['warning'] else cand_row['selection_token']
    light_res = json.loads(_project_cleanup(ctx, 'closed-copies', 'execute',
                                            selections=[cand_row['id']], paths=cand_paths,
                                            confirmations={cand_row['id']: cand_tok}))
    assert light_res['results'][0]['reason'] == 'light_mode_system_repo_blocked' and copies['body_cand'].exists()
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'pro')
    # Protected artifacts block even recorded copies.
    ctx.task_contract = {'resource_policy': {'protected_artifacts': [
        {'id': 'snap-art', 'paths': [str(copies['snap_git'])], 'deny': ['read_bytes', 'delete']},
    ]}}
    prot_plan = json.loads(_project_cleanup(ctx, 'closed-copies'))
    prot_row = next(t for t in prot_plan['targets'] if t['path'] == str(copies['snap_git']))
    assert prot_row['protection'] == 'caller_access_denied' and 'RESOURCE_POLICY_BLOCKED' in prot_row['access_denial']
    ctx.task_contract = {}
    # Corrupt private_copies inventory fails closed.
    (data / 'state/private_copies/corrupt.json').write_text('{bad', encoding='utf-8')
    from ouroboros.tools.project_cleanup import _access
    assert _access(ctx, 'read', str(copies['snap_git']))
    assert (outside_target / 'secret.txt').read_text() == 'keep' and (unrecorded / 'keep.txt').read_text() == 'keep'
