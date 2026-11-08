"""A definite wake-thread start refusal preserves the pending opportunity."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from tests.test_project_attention import runtime, ordinary, lap, episode
from ouroboros import project_attention as attention


@pytest.mark.parametrize('new_admission', [False, True])
def test_post_bind_thread_refusal_recovers_at_existing_floor(runtime, monkeypatch, new_admission):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent, store
    from tests.test_consciousness_wake_lane import _wait_for
    ran = Mock()
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=ran))
    start = threading.Thread.start
    refusing = {'value': True}
    def guarded(thread):
        if thread.name.startswith('wake-turn-') and refusing['value']:
            if new_admission:
                with store.open_store(root, write=True) as txn:
                    attention.record_admission(txn, ordinary(project, 'newer'))
            raise RuntimeError('definite thread start refusal')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', guarded)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not ran.called and not registry.actors()
    assert duty.last_result[project['id']]['reason'] == 'admission_failed'
    if new_admission:
        assert episode(root, project['id'])['admission_witness_id'] == 'newer:1'
    else:
        assert episode(root, project['id'])['pending']
    refusing['value'] = False
    duty._retry_at = 1.0
    lap(duty)
    assert duty.last_result[project['id']]['admitted']
    assert _wait_for(lambda: not registry.actors())
    assert ran.call_count == 1
    lap(duty)
    assert not episode(root, project['id'])['eligible']
