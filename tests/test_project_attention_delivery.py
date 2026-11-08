"""Falsifiers for pre-generation binding publication and quiet launch recovery."""
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from tests.test_project_attention import runtime, ordinary, lap, episode
from ouroboros import project_attention as attention


def test_required_binding_failure_precedes_project_binding_and_typing(runtime, monkeypatch):
    from supervisor import workers
    from ouroboros import agent, projects_registry
    from supervisor.message_bus import get_bridge
    root, project, duty, queue, registry = runtime
    monkeypatch.setattr(agent, 'make_agent', lambda **kw: SimpleNamespace(handle_task=Mock()))
    binding = Mock()
    monkeypatch.setattr(projects_registry, 'bind_task_to_project', binding)
    typing = Mock()
    monkeypatch.setattr(get_bridge(), 'send_chat_action', typing)
    def broken(task):
        raise OSError('required binding unavailable')
    result = workers.handle_wake_direct(project['chat_id'], 'opportunity', {
        'initiator': 'consciousness', 'wake_cause': 'project_idle',
        'consciousness_cause': 'project_idle', 'project_id': project['id']},
        bind_input=broken, bind_required=True)
    assert result['reason'] == 'input_binding_failed'
    assert not binding.called and not typing.called and not registry.actors()


@pytest.mark.parametrize('reason', ['input_binding_failed', 'restart_pending'])
def test_unrepresented_refusal_revisited_without_unrelated_write(runtime, monkeypatch, reason):
    root, project, duty, queue, registry = runtime
    receipt = Mock(return_value={'admitted': False, 'reason': reason})
    monkeypatch.setattr(duty, 'launch', receipt)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)
    before = receipt.call_count
    lap(duty)
    assert receipt.call_count == before + 1
    assert episode(root, project['id'])['pending']

from tests.test_consciousness_allowance import data_root, _at, _settle, _ledger_rows, T0, HOUR
from ouroboros import usage_accounting as ua, usage_store


def test_idle_root_cause_survives_generic_child_and_late_receipts(data_root, monkeypatch):
    from ouroboros.task_results import write_task_result
    _at(monkeypatch, T0 - HOUR)
    write_task_result(data_root, 'idle-root', 'running', root_task_id='idle-root')
    with ua.usage_scope(ua.UsageScope(drive_root=data_root, root_task_id='idle-root', consciousness_cause='project_idle')):
        _settle(data_root, cost=1, cost_final=True, task_id='idle-root', root_task_id='idle-root', category='consciousness')
    with ua.usage_scope(ua.UsageScope(drive_root=data_root, root_task_id='idle-root', consciousness_cause='started')):
        _settle(data_root, cost=.25, cost_final=True, task_id='idle-child', root_task_id='idle-root', category='subagent')
    usage_store.forget(data_root)
    ua.record_subscription_session('idle-late', drive_root=data_root, route='fake:subscription', model='fake',
                                   task_id='idle-late', root_task_id='idle-root', spend_usd=.5, category='subagent')
    rows = list(_ledger_rows(data_root))
    assert {row.get('consciousness_cause') for row in rows} == {'project_idle'}
    usage_store.export_journal(data_root)
    usage_store.migrate_from_journal(data_root)
    assert {row.get('consciousness_cause') for row in _ledger_rows(data_root)} == {'project_idle'}


@pytest.mark.parametrize('reason', ['budget_exhausted', 'cost_accounting_unavailable', 'admission_failed'])
def test_persistent_refusal_rests_until_budget_input_changes(runtime, monkeypatch, reason):
    root, project, duty, queue, registry = runtime
    from ouroboros import config
    limit = {'value': '200'}
    setting = config.runtime_setting
    monkeypatch.setattr(config, 'runtime_setting', lambda key, default=None: limit['value'] if key == 'TOTAL_BUDGET' else setting(key, default))
    receipt = Mock(return_value={'admitted': False, 'reason': reason})
    monkeypatch.setattr(duty, 'launch', receipt)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)  # absorb the one episode write
    before = receipt.call_count
    for _ in range(5): lap(duty)
    assert receipt.call_count == before
    limit['value'] = '300'
    lap(duty)
    assert receipt.call_count == before + 1


def test_unknown_custody_has_typed_deferral_not_false_idle(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    import ouroboros.delegate_custody_current as custody
    monkeypatch.setattr(custody, 'snapshot', lambda *a, **kw: {'pending': [{'task_id': 'missing-owner'}], 'state': {}})
    launch = Mock()
    monkeypatch.setattr(duty, 'launch', launch)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    assert not launch.called
    assert duty.last_result[project['id']]['reason'] == 'current_custody_incomplete'
    assert duty.last_result[project['id']]['gaps'][0]['task_id'] == 'missing-owner'


def test_real_agent_construction_failure_does_not_flood_project(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent as agent_module
    from supervisor import workers
    made = Mock(side_effect=RuntimeError('persistent fixture construction failure'))
    monkeypatch.setattr(agent_module, 'make_agent', made)
    posted = Mock()
    monkeypatch.setattr(workers, 'send_with_budget', posted)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)  # fold's revision changes once
    before = (made.call_count, posted.call_count)
    for _ in range(5): lap(duty)
    assert (made.call_count, posted.call_count) == before
    assert 0 < made.call_count <= 2
    assert posted.call_count == made.call_count
    assert duty.last_result[project['id']]['reason'] == 'admission_failed'


def test_transient_constructor_recovers_at_shared_clock_floor(runtime, monkeypatch):
    root, project, duty, queue, registry = runtime
    from ouroboros import agent as agent_module
    from tests.test_consciousness_wake_lane import _wait_for
    made = Mock(side_effect=RuntimeError('transient constructor fault'))
    monkeypatch.setattr(agent_module, 'make_agent', made)
    attention.note_admission(root, ordinary(project))
    lap(duty)
    lap(duty)
    before = made.call_count
    assert duty._retry_at > 0
    for _ in range(5): lap(duty)
    assert made.call_count == before
    class Actor:
        def handle_task(self, task):
            return []
    made.side_effect = None
    made.return_value = Actor()
    duty._retry_at = 1.0  # controlled expiry; no new admission/store/budget input
    lap(duty)
    assert made.call_count == before + 1
    assert duty.last_result[project['id']]['admitted']
    tid = duty.last_result[project['id']]['task_id']
    assert _wait_for(lambda: registry.get(tid) is None)
    lap(duty)
    assert not episode(root, project['id'])['eligible']


def test_current_claim_read_works_before_optional_index_exists(runtime):
    root, project, duty, queue, registry = runtime
    from ouroboros import store
    with store.open_store(root, write=True) as txn:
        txn.conn.execute('DROP INDEX IF EXISTS task_handoffs_launch')
    owners, gaps = attention.current_owners(root, [], [])
    assert owners == {} and gaps == []


def test_persistent_observation_fault_warns_once_and_recovers(runtime, monkeypatch, caplog):
    from ouroboros import projects_registry
    root, project, duty, queue, registry = runtime
    original = projects_registry.list_projects
    fault = {'text': 'registry unavailable'}
    def read(*args, **kwargs):
        if fault['text']:
            raise ValueError(fault['text'])
        return original(*args, **kwargs)
    monkeypatch.setattr(projects_registry, 'list_projects', read)
    launches = Mock(return_value={'admitted': False})
    monkeypatch.setattr(duty, 'launch', launches)
    attention.note_admission(root, ordinary(project))
    for _ in range(4): lap(duty)
    assert not launches.called
    warnings = lambda: [r for r in caplog.records if r.name == attention.log.name and r.levelname == 'WARNING']
    assert len(warnings()) == 1
    fault['text'] = 'different registry fault'
    lap(duty)
    assert len(warnings()) == 2
    fault['text'] = ''
    lap(duty)
    assert launches.call_count == 1
    fault['text'] = 'registry unavailable'
    lap(duty)
    assert len(warnings()) == 3
