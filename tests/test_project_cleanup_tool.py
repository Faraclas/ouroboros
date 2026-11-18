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
    assert 'physical denial' in _project_cleanup(ctx, 'room')
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
