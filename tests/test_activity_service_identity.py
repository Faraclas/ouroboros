"""Live Works and retained activity share the same exact service-start spelling."""
import pytest
from ouroboros.activity_history import activity_sources, retained_activity
from ouroboros.gateway.work_projection import _typed_services


@pytest.mark.parametrize('start', [1767225600, 1767225600.0, 1767225600.125,
    '2026-01-01T00:00:00Z', '2026-01-01T01:00:00+01:00'])
def test_live_and_retained_start_identity(start):
    service = {'task_id': 'x', 'service_id': 'x:build', 'name': 'Build',
               'started_at': start, 'observed_at': 1767225601.0,
               'state': 'running', 'returncode': None}
    root = {'task_id': 'x', 'status': 'running', 'service_observations': {'services': [service]}}
    kept = [e for e in activity_sources(root) if e['kind'] == 'service'][0]
    live = _typed_services([service], 1767225602.0)[0]
    assert kept['key'] == f"service:{live['task_id']}:{live['service_id']}:{live['started_at']}"
    assert kept['at'] == kept['service']['started_at'] == live['started_at']
    # Projection must not rewrite persisted owner observations.
    assert service['started_at'] == start


def test_replacement_starts_remain_separate():
    services = [{'task_id': 'x', 'service_id': 'x:build', 'name': 'Build',
                 'started_at': start, 'observed_at': 1767225610.0, 'state': 'exited', 'returncode': 7}
                for start in (1767225600.0, 1767225601.0)]
    root = {'task_id': 'x', 'status': 'running', 'service_observations': {'services': services}}
    keys = [e['key'] for e in activity_sources(root) if e['kind'] == 'service']
    assert len(keys) == len(set(keys)) == 2


def test_malformed_start_is_an_explicit_retained_gap():
    root = {'task_id': 'x', 'status': 'running', 'service_observations': {'services': [
        {'task_id': 'x', 'service_id': 'x:build', 'started_at': 'not-a-date', 'state': 'running'}]}}
    kept = retained_activity(root)
    assert len(kept['entries']) == 1  # root only; no fabricated service occurrence
    assert kept['gaps'] == ['Some service observations have no exact start identity']
