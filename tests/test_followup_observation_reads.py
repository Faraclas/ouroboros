"""Schedule observations reuse strict documents, never mutation authority."""
from __future__ import annotations

import copy
from collections import Counter
from types import SimpleNamespace

import pytest

from ouroboros import store, task_results
from supervisor import followup_policy as policy
from tests._store_shared import seed_result


@pytest.fixture
def records(tmp_path):
    origin = {'_schema_version': 1, 'task_id': 'origin', 'root_task_id': 'origin', 'status': 'completed'}
    fired = {'_schema_version': 1, 'task_id': 'fired', 'root_task_id': 'origin', 'status': 'completed'}
    for document in (origin, fired):
        seed_result(tmp_path, document['task_id'], document)
    row = {'id': 'schedule', 'source': 'task_followup', 'last_task_id': 'fired',
           'followup_origin': {'task_id': 'origin', 'root_task_id': 'origin'},
           'followup_relation': {'kind': 'related', 'revision': 'one', 'billing_group': {
               'billing_group_limit_source': 'test', 'billing_group_limit_revision': 'one',
               'billing_group_limit_usd': 20}}}
    return tmp_path, origin, fired, row


def observe(root, row):
    return policy.observed_store(root, {'tasks': [row]})['tasks'][0]


def test_observation_materializes_each_referenced_document_once(records, monkeypatch):
    root, _, _, row = records
    calls = Counter()
    original = task_results.load_task_result

    def counted(*args, **kwargs):
        assert kwargs == {'strict': True, 'signed': True}
        calls[args[1]] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(task_results, 'load_task_result', counted)
    actual = observe(root, row)
    assert actual['followup_wait'] == '' and not actual.get('followup_hold')
    assert calls == {'origin': 1, 'fired': 1}


def test_next_row_and_next_observation_read_new_body_and_controls(records, monkeypatch):
    root, origin, _, row = records
    original = policy.policy_view
    count = 0

    def publish_after_first(*args, **kwargs):
        nonlocal count
        view = original(*args, **kwargs)
        count += 1
        if count == 1:
            seed_result(root, 'origin', origin | {'followup_stop': {'control_id': 'new'}})
        return view

    monkeypatch.setattr(policy, 'policy_view', publish_after_first)
    rows = policy.observed_store(root, {'tasks': [row, copy.deepcopy(row)]})['tasks']
    assert not rows[0].get('followup_hold')
    assert rows[1]['followup_hold']['controls'] == {'stop:origin': 'new'}
    assert observe(root, row)['followup_hold']['controls'] == {'stop:origin': 'new'}


def test_observation_does_not_replace_mutation_or_relationship_readers(records, monkeypatch):
    root, origin, _, row = records
    assert not observe(root, row).get('followup_hold')
    seed_result(root, 'origin', origin | {'followup_stop': {'control_id': 'after-view'}})
    assert policy.policy_view(root, {}, row)['hold']['controls'] == {'stop:origin': 'after-view'}
    calls = []
    original = task_results.load_task_result

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(task_results, 'load_task_result', counted)
    policy.policy_view(root, {}, row)
    assert calls.count('origin') == 2  # default authority path is unchanged
    with pytest.raises(ValueError, match='followup_origin_unavailable'):
        policy.resolve_relation(root, {'task_id': 'missing', 'root_task_id': 'missing'}, 'related', declared_by='test')
    assert 'missing' in calls


def retry(root, count):
    for index in range(count + 1):
        tid = 'origin' if index == 0 else f'retry-{index}'
        prior = 'origin' if index == 1 else f'retry-{index - 1}'
        doc = {'_schema_version': 1, 'task_id': tid, 'root_task_id': 'origin', 'status': 'completed'}
        if index:
            doc.update(parent_task_id='', delegation_role='root', original_task_id=prior,
                       timeout_retry_from=prior, supersedes_task_id=prior)
        if index < count:
            doc.update(superseded_by=f'retry-{index + 1}', retry_task_id=f'retry-{index + 1}')
        else:
            doc['followup_stop'] = {'control_id': 'leaf-stop'}
        seed_result(root, tid, doc)


@pytest.mark.parametrize('shape', ['valid', 'broken', 'cycle', 'bound'])
def test_retry_discovery_and_integrity_preserve_strict_view(records, monkeypatch, shape):
    from ouroboros import cancel_intents
    root, _, _, row = records
    monkeypatch.setattr(cancel_intents, '_RETRY_LINEAGE_MAX_HOPS', 2)
    retry(root, 3 if shape == 'bound' else 2)
    if shape == 'broken':
        seed_result(root, 'retry-1', task_results.load_task_result(root, 'retry-1') | {'original_task_id': 'wrong'})
    if shape == 'cycle':
        seed_result(root, 'retry-2', task_results.load_task_result(root, 'retry-2') |
                    {'superseded_by': 'origin', 'retry_task_id': 'origin'})
    expected = policy.policy_view(root, {}, row)
    observed = observe(root, row)
    assert observed['followup_wait'] == expected['wait']
    assert observed.get('followup_hold') == expected.get('hold')
    if shape == 'valid':
        assert observed['followup_hold']['controls'] == {'stop:retry-2': 'leaf-stop'}
    else:
        assert observed['followup_wait'] == 'followup_authority_unavailable'


@pytest.mark.parametrize('body', ['{bad', 'null', '[]', '{"_schema_version":1,"task_id":"other","status":"completed"}',
                                '{"_schema_version":1,"task_id":"origin","status":0}', b'{bad', None])
def test_strict_malformed_blob_and_occupied_absence(records, body):
    root, _, _, row = records
    with store.open_store(root, write=True) as tx:
        if body is None:
            tx.conn.execute('DELETE FROM task_bodies WHERE task_id=?', ('origin',))
        else:
            tx.conn.execute('UPDATE task_bodies SET body=? WHERE task_id=?', (body, 'origin'))
    expected = policy.policy_view(root, {}, row)
    actual = observe(root, row)
    assert actual['followup_wait'] == expected['wait'] == 'followup_authority_unavailable'
    assert actual.get('followup_hold') == expected.get('hold')


@pytest.mark.parametrize('error', [store.StoreBusy('busy', reason='contention'), ValueError('bad source')])
def test_read_errors_are_not_absence_and_retry_next_call(records, monkeypatch, error):
    root, _, _, row = records
    with monkeypatch.context() as m:
        m.setattr(task_results, 'load_task_result', lambda *a, **k: (_ for _ in ()).throw(error))
        actual = observe(root, row)
        expected = policy.policy_view(root, {}, row)
        assert actual['followup_wait'] == expected['wait'] == 'followup_authority_unavailable'
    assert observe(root, row)['followup_wait'] == ''


def test_context_uses_observation_reader(records, monkeypatch):
    from ouroboros.context_runtime_facts import schedule_store_fact
    from supervisor import queue
    root, _, _, row = records
    monkeypatch.setattr(queue, 'load_schedule_store', lambda drive: {'tasks': [row]})
    result = schedule_store_fact(SimpleNamespace(drive_root=root))
    assert result == policy.observed_store(root, {'tasks': [row]})


def test_writer_after_first_load_invalidates_reuse_within_row(records, monkeypatch):
    root, origin, _, row = records
    original = task_results.load_task_result
    calls = Counter()

    def publish(*args, **kwargs):
        answer = original(*args, **kwargs)
        calls[args[1]] += 1
        if args[1] == 'origin' and calls['origin'] == 1:
            # Commit after the captured read, before the validator returns.
            seed_result(root, 'origin', origin | {'followup_stop': {'control_id': 'transferred'}})
        return answer

    monkeypatch.setattr(task_results, 'load_task_result', publish)
    actual = observe(root, row)
    assert actual['followup_hold']['controls'] == {'stop:origin': 'transferred'}
    assert calls['origin'] == 2


def test_new_cancel_intent_after_body_read_is_not_memoized(records, monkeypatch):
    from ouroboros.cancel_intents import request_cancel
    root, _, _, row = records
    original = task_results.load_task_result
    fired = False

    def publish(*args, **kwargs):
        nonlocal fired
        answer = original(*args, **kwargs)
        if args[1] == 'origin' and not fired:
            fired = True
            request_cancel(root, 'origin', source='owner', allow_settled_target=True)
        return answer

    monkeypatch.setattr(task_results, 'load_task_result', publish)
    actual = observe(root, row)
    assert 'stop:origin' in actual['followup_hold']['controls']


def test_signature_contention_stays_unknown(records, monkeypatch):
    from ouroboros import task_store
    root, _, _, row = records
    original = task_store.signature
    calls = 0
    def unavailable(*a, **k):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise store.StoreBusy('signature busy', reason='contention')
        return original(*a, **k)
    monkeypatch.setattr(task_store, 'signature', unavailable)
    actual = observe(root, row)
    assert actual['followup_wait'] == 'followup_authority_unavailable'
    assert calls == 2 and not actual.get('followup_hold')


def test_valid_blob_and_legacy_absence_are_not_display_fallbacks(records):
    import json
    root, origin, _, row = records
    with store.open_store(root, write=True) as tx:
        tx.conn.execute('UPDATE task_bodies SET body=? WHERE task_id=?', (json.dumps(origin).encode(), 'origin'))
    expected = policy.policy_view(root, {}, row)
    assert observe(root, row)['followup_wait'] == expected['wait']
    from tests._store_shared import delete_result
    delete_result(root, 'origin')
    legacy = {'id': 'legacy', 'source': 'task_followup', 'followup_origin': row['followup_origin']}
    assert task_results.load_task_result(root, 'origin', strict=True) is None
    assert observe(root, legacy)['followup_wait'] == policy.policy_view(root, {}, legacy)['wait']


@pytest.mark.parametrize('count,wait', [(32, ''), (33, 'followup_authority_unavailable')])
def test_actual_retry_integrity_bound(records, count, wait):
    root, _, _, row = records
    retry(root, count)
    expected = policy.policy_view(root, {}, row)
    actual = observe(root, row)
    assert actual['followup_wait'] == expected['wait'] == wait
    assert actual.get('followup_hold') == expected.get('hold')


def test_occupied_missing_identity_keeps_strict_refusal(records):
    from ouroboros.store_import import OCCUPIED
    from tests._store_shared import delete_result
    root, _, _, row = records
    delete_result(root, 'origin')
    with store.open_store(root, write=True) as tx:
        tx.set_meta(OCCUPIED, {'origin': 'quarantined_before_import'})
    with pytest.raises(ValueError, match='unreadable or invalid'):
        task_results.load_task_result(root, 'origin', strict=True)
    assert observe(root, row)['followup_wait'] == policy.policy_view(root, {}, row)['wait'] == 'followup_authority_unavailable'


def test_deletion_after_first_read_is_not_cached_authority(records, monkeypatch):
    from tests._store_shared import delete_result
    root, _, _, row = records
    original = task_results.load_task_result
    deleted = False
    def load(*a, **k):
        nonlocal deleted
        value = original(*a, **k)
        if a[1] == 'origin' and not deleted:
            deleted = True
            delete_result(root, 'origin')
        return value
    monkeypatch.setattr(task_results, 'load_task_result', load)
    assert observe(root, row)['followup_wait'] == 'followup_authority_unavailable'


@pytest.mark.parametrize('observation', [False, True])
def test_stop_transfer_after_document_read_retains_existing_race(records, monkeypatch, observation):
    """Neither path promises a linearizable row+intent snapshot; the next read sees it."""
    from ouroboros import cancel_intents, task_store
    root, _, _, row = records
    intent = cancel_intents.request_cancel(root, 'origin', source='owner', allow_settled_target=True)
    original_load, original_signature = task_results.load_task_result, task_store.signature
    reads = 0
    def transfer():
        assert cancel_intents.settle_intent(root, 'origin', outcome='already_settled',
                                           request_id=intent['request_id'])
    def load(*a, **k):
        nonlocal reads
        value = original_load(*a, **k)
        if a[1] == 'origin':
            reads += 1
            if reads == 2:
                transfer()
        return value
    def signature(*a, **k):
        nonlocal reads
        value = original_signature(*a, **k)
        if a[1] == 'origin':
            reads += 1
            if reads == 2:
                transfer()
        return value
    with monkeypatch.context() as m:
        m.setattr(task_store if observation else task_results,
                  'signature' if observation else 'load_task_result', signature if observation else load)
        result = observe(root, row) if observation else policy.policy_view(root, {}, row)
    assert reads == 2
    assert not result.get('followup_hold' if observation else 'hold')
    assert observe(root, row)['followup_hold']['controls'] == {'stop:origin': intent['followup_stop']['control_id']}
