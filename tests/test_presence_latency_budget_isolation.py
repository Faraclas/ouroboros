"""Latency harness must not borrow another test's depleted supervisor wallet."""
import pytest
from ouroboros import usage_accounting as ua
from supervisor import state
from tests.test_presence_terminal_latency import (
    agent_bootstrap,
    test_native_agent_returns_durable_result_before_synthesis_and_retry_reuses_it as run_latency,
)


@pytest.mark.parametrize('outcome', ['message', 'silent', 'tool_delivered'])
def test_latency_harness_keeps_real_admission_on_its_own_wallet(tmp_path, monkeypatch, outcome):
    foreign = tmp_path / 'foreign'
    scope = ua.UsageScope(drive_root=foreign, task_id='foreign-latency-budget', non_task_operation=True)
    with ua.usage_scope(scope):
        reservation = ua.reserve_attempt(ua.AttemptRequest(
            model='fixture', provider='openai-compatible', reservation_usd=0.,
            drive_root=foreign, task_id='foreign-latency-budget'))
        ua.mark_dispatched(reservation)
        ua.settle_attempt(reservation, {'prompt_tokens':1,'completion_tokens':1},
                          cost_usd=1000., cost_final=True)
    for name, value in {
        'DRIVE_ROOT':foreign, 'STATE_PATH':foreign/'state/state.json',
        'STATE_LAST_GOOD_PATH':foreign/'state/state.last_good.json',
        'STATE_LOCK_PATH':foreign/'locks/state.lock', 'TOTAL_BUDGET_LIMIT':100.,
    }.items():
        monkeypatch.setattr(state, name, value)
    assert state.budget_remaining({}, strict=True)==0
    target=tmp_path/'target'; target.mkdir()
    agent_bootstrap.__wrapped__(target, monkeypatch)
    run_latency(target, monkeypatch, outcome, None)
    assert state.DRIVE_ROOT==target and state.TOTAL_BUDGET_LIMIT==100.
    assert state.budget_remaining({}, strict=True)>0
    from ouroboros.usage_store import hold
    with hold(foreign) as view:
        row=view.attempt(reservation.attempt_id)
    assert row['state']=='settled' and row['cost_usd']==1000.
