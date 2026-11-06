"""A bootstrap fixture must not read another test's depleted global wallet."""
import pytest

from ouroboros import usage_accounting as ua
from supervisor import state
from tests.test_presence_continuation_bootstrap import (
    test_full_agent_bootstrap_propagates_review_wait_and_reenters_same_author as run_bootstrap,
)


@pytest.mark.parametrize('review_mode', ['auto', 'required'])
def test_bootstrap_uses_its_own_data_not_a_depleted_shared_wallet(tmp_path, monkeypatch, review_mode):
    foreign = tmp_path / 'foreign'
    scope = ua.UsageScope(drive_root=foreign, task_id='foreign-budget-fixture', non_task_operation=True)
    with ua.usage_scope(scope):
        request = ua.AttemptRequest(model='fixture', provider='openai-compatible', reservation_usd=0.,
                                    drive_root=foreign, task_id='foreign-budget-fixture')
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        ua.settle_attempt(reservation, {'prompt_tokens': 1, 'completion_tokens': 1}, cost_usd=1000., cost_final=True)
    # A real isolated ledger charge, not a mocked budget_remaining or bypassed gate.
    for name, value in {
        'DRIVE_ROOT': foreign, 'STATE_PATH': foreign/'state/state.json',
        'STATE_LAST_GOOD_PATH': foreign/'state/state.last_good.json',
        'STATE_LOCK_PATH': foreign/'locks/state.lock', 'TOTAL_BUDGET_LIMIT': 100.,
    }.items(): monkeypatch.setattr(state, name, value)
    assert state.budget_remaining({}, strict=True) == 0
    target = tmp_path / 'target'
    target.mkdir()
    run_bootstrap(target, monkeypatch, review_mode)
    assert state.DRIVE_ROOT == target/'data'
    assert state.budget_remaining({}, strict=True) > 0
    # The fixture does not refill, remove or edit the unrelated wallet.
    from ouroboros.usage_store import hold
    with hold(foreign) as view:
        row = view.attempt(reservation.attempt_id)
    assert row['cost_usd'] == 1000. and row['state'] == 'settled'
