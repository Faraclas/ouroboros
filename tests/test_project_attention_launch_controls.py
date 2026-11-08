"""Controls changed while preparing project attention still govern registration."""
import pytest
from unittest.mock import Mock
from types import SimpleNamespace
from tests.test_project_attention import runtime, ordinary, lap, episode
from ouroboros import project_attention as attention

@pytest.mark.parametrize('control', ['stop', 'off', 'panic'])
def test_control_changed_during_allowance_prevents_registration(runtime, monkeypatch, control):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent
    seen = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    enabled = {'value': True}
    panic = {'value': False}
    duty.clock._read_state = lambda: {'bg_consciousness_enabled': enabled['value']}
    monkeypatch.setattr('ouroboros.consciousness.panic_blocks_wake', lambda root: panic['value'])
    def allowance(*args, **kwargs):
        if kwargs.get('fresh'):
            if control == 'stop': duty.clock._stopped = True
            elif control == 'off': enabled['value'] = False
            else: panic['value'] = True
        return {'status': 'available', 'remaining_usd': 10}
    duty.clock._allowance_view = allowance
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not seen.called
    assert not registry.actors()
    assert episode(root, project['id'])['pending']
    assert duty.clock._next_wake_at == 1234

@pytest.mark.parametrize('change', ['ordinary', 'stop'])
def test_current_owner_changes_after_last_snapshot_prevents_body(runtime, monkeypatch, change):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent
    seen = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=seen))
    original = attention.current_owners
    calls = []
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            if change == 'ordinary': queue.PENDING.append(ordinary(project, 'late'))
            else: duty.clock._stopped = True
        return result
    monkeypatch.setattr(attention, 'current_owners', changed)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert len(calls) == 2
    assert not seen.called
    assert not registry.actors()
    assert episode(root, project['id'])['pending']

from ouroboros.usage_store import decode_attempts as real_decode_attempts

def test_usage_only_reservation_release_invalidates_idle_observation(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    from ouroboros import usage_accounting as ua, usage_store, store
    from tests._store_shared import seed_result
    seed_result(root, 'usage-owner', {'status': 'completed', 'chat_id': project['chat_id'], 'project_id': project['id']})
    monkeypatch.setattr(usage_store, 'decode_attempts', real_decode_attempts)
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id='usage-owner', root_task_id='usage-owner')):
        held = ua.reserve_attempt(ua.AttemptRequest(model='fixture', provider='test', reservation_usd=None))
    attention.note_admission(root, ordinary(project))
    launches = Mock(return_value={'admitted': False})
    monkeypatch.setattr(duty, 'launch', launches)
    lap(duty)
    lap(duty)
    assert not launches.called
    with store.open_store(root, write=False) as txn:
        revision = txn.revision()
    ua.release_attempt(held, reason='fixture_not_dispatched')
    with store.open_store(root, write=False) as txn:
        assert txn.revision() == revision
    lap(duty)
    assert launches.call_count == 1
