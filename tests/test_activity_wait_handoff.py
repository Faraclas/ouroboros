"""Chooser observations are queue facts, not inherited worker payload state."""
from supervisor.worker_assignment import _settled_running_task
from ouroboros.gateway.work_status import classify_member


def test_running_handoff_drops_only_queue_diagnostic():
    row = {'id': 'q', '_attempt': 2, 'dispatch_wait': {'code': 'dispatch_ready'}, 'text': 'owned'}
    running = _settled_running_task(row, {})
    assert 'dispatch_wait' not in running
    assert running['text'] == 'owned' and running['_attempt'] == 2
    assert row['dispatch_wait'] == {'code': 'dispatch_ready'}


def test_chooser_observation_time_is_not_wait_start():
    raw = {'dispatch_wait': {'code': 'project_busy', 'ids': ['owner'],
                            'task_attempt': 2, 'observed_at': '2026-01-01T00:01:00Z'}}
    reason = classify_member({'activity_id': 'q', 'phase': 'queued', 'task_attempt': 2}, raw)['reasons'][0]
    assert reason['observed_at'] == '2026-01-01T00:01:00Z'
    assert 'since' not in reason
