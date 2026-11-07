"""A stale same-task execution replica cannot erase retained service starts."""
from tests._store_shared import seed_result
from ouroboros.activity_history import room_activity_page
from ouroboros.task_status import effective_task_result


def test_history_keeps_canonical_second_start_against_older_replica(tmp_path):
    replica = tmp_path / 'execution'
    old = {'service_id': 'svc', 'name': 'old', 'started_at': '2026-01-01T00:00:00Z',
           'observed_at': '2026-01-01T00:01:00Z', 'state': 'exited', 'returncode': 7}
    new = {**old, 'name': 'new', 'started_at': '2026-01-01T00:02:00Z',
           'observed_at': '2026-01-01T00:03:00Z', 'returncode': -9}
    row = seed_result(tmp_path, 'observed', {'status': 'completed', 'chat_id': 1,
        'child_drive_root': str(replica), 'service_observations': {'services': [old, new]}})
    seed_result(replica, 'observed', {'status': 'completed', 'chat_id': 1,
        'service_observations': {'services': [old]}})
    effective = effective_task_result(tmp_path, row, materialize_artifacts=False, follow_successors=False)
    assert effective['service_observations']['services'] == [old, new]
    services = [e for e in room_activity_page(tmp_path, 1)['tasks'][0]['entries'] if e['kind'] == 'service']
    assert {e['name'] for e in services} == {'old', 'new'}
    assert not any(e['error'] for e in services)
    from ouroboros.task_results import write_task_result, load_task_result
    write_task_result(tmp_path, 'observed', 'completed', service_observations={'services': [old]})
    assert load_task_result(tmp_path, 'observed')['service_observations']['services'] == [old, new]


def test_same_start_newer_probe_not_older_replica_selects_values():
    from ouroboros.service_observations import merge_observations
    live = {'service_id': 'svc', 'started_at': 'start', 'state': 'live',
            'observed_at': '2026-01-01T01:00:00+01:00'}
    ended = {**live, 'state': 'exited', 'returncode': -9, 'observed_at': '2026-01-01T00:01:00Z'}
    for a, b in ((live, ended), (ended, live)):
        assert merge_observations({'services': [a]}, {'services': [b]})['services'] == [ended]


def test_actual_numeric_observation_producer_and_unknowns():
    from ouroboros.service_observations import merge_observations
    old = {'service_id': 'svc', 'started_at': 10, 'state': 'live', 'observed_at': 20.0}
    new = {**old, 'state': 'exited', 'observed_at': 30.0}
    assert merge_observations({'services': [old]}, {'services': [new]})['services'] == [new]
    assert merge_observations({'services': [new, 'gap']}, {'services': [old, 'gap']})['services'] == [new, 'gap']


def test_new_attempt_envelope_and_old_live_start_do_not_claim_current_liveness(tmp_path):
    from ouroboros.task_results import write_task_result, load_task_result
    from ouroboros.gateway.work_projection import _service_tails
    from types import SimpleNamespace
    old = {'service_id': 'old', 'started_at': 10.0, 'state': 'live', 'observed_at': 20.0}
    new = {'service_id': 'new', 'started_at': 30.0, 'state': 'exited', 'observed_at': 40.0, 'returncode': 7}
    seed_result(tmp_path, 'retrying', {'status': 'running', 'task_attempt': 2,
        'service_observations': {'task_id': 'retrying', 'task_attempt': 1, 'observed_at': 20.0, 'services': [old]}})
    write_task_result(tmp_path, 'retrying', 'running', service_observations={
        'task_id': 'retrying', 'task_attempt': 2, 'observed_at': 40.0, 'services': [new]})
    current = load_task_result(tmp_path, 'retrying')
    assert current['service_observations']['task_attempt'] == 2
    assert len(current['service_observations']['services']) == 2
    tails = _service_tails('retrying', 2, {}, {}, SimpleNamespace(result=lambda _id: current))
    assert [t['service_id'] for t in tails] == ['new']
    assert tails[0]['state'] == 'ended' and tails[0]['returncode'] == 7


def test_unknown_retained_service_rows_do_not_break_current_census():
    from ouroboros.gateway.work_projection import _service_tails
    from types import SimpleNamespace
    service = {'service_id': 'known', 'started_at': 10.0, 'state': 'exited',
               'observed_at': 20.0, 'returncode': 7}
    envelope = {'task_id': 'owner', 'task_attempt': 1, 'services': ['gap', service]}
    for stored, view in ((envelope, {}), ({}, envelope)):
        tails = _service_tails('owner', 1, {}, {'service_observations': view},
                               SimpleNamespace(result=lambda _id: {'service_observations': stored}))
        assert [tail['service_id'] for tail in tails] == ['known']
        assert tails[0]['state'] == 'ended'
