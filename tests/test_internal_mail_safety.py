"""Internal mail keeps host boundaries without a second model authorization."""
import pytest
from ouroboros import safety
from ouroboros.owner_mailbox import drain_owner_entries
from ouroboros.tools.registry import ToolRegistry
from tests.test_steer_relay import _live_tool_ctx, _supervisor_ctx


def test_registry_tell_keeps_quote_provenance_and_real_supervisor_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'pro')
    monkeypatch.setenv('OUROBOROS_SAFETY_MODE', 'full')
    monkeypatch.setattr(safety, '_run_llm_check', lambda *a, **k: pytest.fail('internal mail must not call a model'))
    import supervisor.queue as queue
    monkeypatch.setattr(queue, 'DRIVE_ROOT', str(tmp_path))
    emitted = []
    donor = _live_tool_ctx(tmp_path, _supervisor_ctx(tmp_path, []), emitted)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.__dict__.update(donor.__dict__)
    registry._ctx.task_metadata['work_kind'] = 'owner'
    refused = registry.execute('tell_task', {'task_id': 't-target', 'note': 'my note', 'quote': ['row:made-up']})
    assert 'QUOTE_NOT_HELD' in refused and not emitted
    result = registry.execute('tell_task', {'task_id': 't-target', 'note': 'my note'})
    assert 'Message written to' in result, result
    [entry] = drain_owner_entries(tmp_path, 't-target')
    assert entry['kind'] == 'relay_entry' and entry['text'] == 'my note'
    assert entry['owner_rows'] == []


def test_registry_forward_keeps_tree_and_terminal_recipient_checks(tmp_path, monkeypatch):
    from ouroboros.task_results import write_task_result
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'pro')
    monkeypatch.setenv('OUROBOROS_SAFETY_MODE', 'full')
    monkeypatch.setattr(safety, '_run_llm_check', lambda *a, **k: pytest.fail('internal mail must not call a model'))
    write_task_result(tmp_path, 'parent', 'running', root_task_id='parent')
    write_task_result(tmp_path, 'child', 'running', parent_task_id='parent', root_task_id='parent', delegation_role='subagent')
    write_task_result(tmp_path, 'ended', 'completed', parent_task_id='parent', root_task_id='parent', delegation_role='subagent')
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'parent'
    assert 'TASK_NOT_ACTIVE' in registry.execute('forward_to_worker', {'task_id': 'ended', 'message': 'context'})
    result = registry.execute('forward_to_worker', {'task_id': 'child', 'message': 'context'})
    assert result == 'Message forwarded to task child', result
    [entry] = drain_owner_entries(tmp_path, 'child')
    assert entry['kind'] == 'task_message' and entry['text'] == 'context'
    assert entry['provenance'] == 'ancestor_task'
    assert not drain_owner_entries(tmp_path, 'ended')


def test_outward_and_opaque_calls_still_reach_the_model_gate(tmp_path, monkeypatch):
    from ouroboros.tools.tool_context import ToolContext
    monkeypatch.setattr(safety, 'get_safety_mode', lambda *a: 'full')
    called = []
    def assess(tool, *a, **k):
        called.append(tool)
        return safety._Assessment(True, '')
    monkeypatch.setattr(safety, '_run_llm_check', assess)
    actor = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id='sender', task_metadata={'work_kind': 'owner'})
    for tool in ('github_write', 'opaque_extension', 'schedule_followup'):
        assert safety.check_safety(tool, {}, ctx=actor)[0]
    assert called == ['github_write', 'opaque_extension', 'schedule_followup']
