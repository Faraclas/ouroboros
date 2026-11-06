"""Requested cache tier, native submission and provider report are distinct facts.

Real context producers/public clients/SDK/accounting; native HTTP is mocked.
No test establishes service acceptance, retention or a cache-performance gain.
"""
import copy
import json
from types import SimpleNamespace

import pytest

from ouroboros import config, llm_observability as obs
from ouroboros.context_fit import ContextCore, _system_blocks, seal_task_transcript
from ouroboros.llm import LLMClient
from ouroboros.loop_llm_call import _record_round_cache_facts
from ouroboros.tools.control_task_results import cache_horizon_note
from ouroboros.usage_store import hold
from tests.test_copilot_option_fidelity import option_wire  # noqa: F401


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('ttl', ['default', '5m', '1h'])
def test_public_cache_evidence_reaches_settlement_and_wait(option_wire, monkeypatch, asynchronous, stream, ttl):
    invoke, sent, _, root, task = option_wire
    monkeypatch.setattr(config, 'resolve_prompt_cache_ttl', lambda: ttl)
    core = ContextCore('governance', 'constitution', '', '', 'identity', 'runtime', '[]', False)
    messages = [{'role': 'system', 'content': _system_blocks(core, ('GOV\n', ''), mode='max')},
                {'role': 'user', 'content': 'Fixture task'}]
    seal_task_transcript(messages)
    original = copy.deepcopy(messages)
    from ouroboros import net_transport
    hm = net_transport._openai_httpx_module()
    original_handle = hm.HTTPTransport.handle_request
    def handle(self, request):
        body = original_handle(self, request).json()
        body['usage']['input_tokens_details'] = {'cached_tokens': 7}
        return hm.Response(200, json=body)
    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', ahandle)
    public = LLMClient.chat_async if asynchronous else LLMClient.chat
    if asynchronous:
        async def entry(self, **kw):
            kw['messages'] = messages
            return await public(self, **kw)
        monkeypatch.setattr(LLMClient, 'chat_async', entry)
    else:
        def entry(self, **kw):
            kw['messages'] = messages
            return public(self, **kw)
        monkeypatch.setattr(LLMClient, 'chat', entry)
    message, usage = invoke(asynchronous=asynchronous, stream=stream)
    assert messages == original and json.loads(message['content']) == {'ok': True}
    assert len(sent) == 1 and 'prompt_cache_options' not in sent[0]['body']
    assert usage['prompt_cache_ttl'] == ttl  # requested candidate tier, NOT retention
    evidence = usage['prompt_cache_ttl_evidence']
    assert evidence == {'requested': ttl, 'request_boundary': 'sdk_input',
                        'http': {'breakpoints': 2, 'key_present': False, 'options_present': False},
                        'provider_reported': None}
    assert usage['request_wire']['observed_http']['cache'] == evidence['http']
    with hold(root) as view:
        row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['state'] == 'settled' and row['prompt_cache_ttl'] == ttl
    assert row['prompt_cache_ttl_evidence'] == evidence
    from ouroboros.usage_accounting import last_task_cache_split, usage_scope, UsageScope
    # The seven observed cached tokens establish no future lifetime, even 1h.
    assert row['cached_tokens'] == 7
    with usage_scope(UsageScope(task_id=task, category='verification', non_task_operation=True)):
        assert last_task_cache_split(task, row['model'], provider=row['provider']) is None
    accumulated = {}
    _record_round_cache_facts(accumulated, usage, round_idx=1)
    assert accumulated['_last_prompt_cache_ttl_evidence'] == evidence
    assert cache_horizon_note(SimpleNamespace(_accumulated_usage=accumulated), 4000) == ''


def test_provider_extension_cannot_replace_host_tier_or_http_evidence(option_wire, monkeypatch):
    invoke, _, state, root, task = option_wire
    hm = __import__('ouroboros.net_transport', fromlist=['x'])._openai_httpx_module()
    original_handle = hm.HTTPTransport.handle_request
    def handle(self, request):
        response = original_handle(self, request)
        body = response.json()
        body['usage'].update(prompt_cache_ttl='provider-only',
                             prompt_cache_ttl_evidence={'http': {'breakpoints': 99}})
        return hm.Response(200, json=body)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
    message, usage = invoke()
    assert 'prompt_cache_ttl' not in usage  # this fixture has no marked messages/tools
    assert usage['prompt_cache_ttl_evidence']['provider_reported'] == 'provider-only'
    assert usage['prompt_cache_ttl_evidence']['http']['breakpoints'] == 0
    with hold(root) as view:
        row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['prompt_cache_ttl'] == ''
    assert row['prompt_cache_ttl_evidence'] == usage['prompt_cache_ttl_evidence']


def test_missing_observation_is_unknown_not_omission(option_wire, monkeypatch):
    invoke, _, _, root, task = option_wire
    monkeypatch.setattr(obs, 'note_http_projection', lambda request: None)
    _, usage = invoke()
    assert usage['prompt_cache_ttl_evidence']['http'] is None
    assert usage['request_wire']['observed_http_gap'] == 'matching_observation_unavailable'
    with hold(root) as view:
        row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['prompt_cache_ttl_evidence']['http'] is None


@pytest.mark.parametrize('ttl', ['5m', '1h'])
def test_unobserved_legacy_tier_is_explicitly_a_request_estimate(ttl):
    usage = {'prompt_cache_ttl': ttl}
    accumulated = {}
    _record_round_cache_facts(accumulated, usage, round_idx=1)
    note = cache_horizon_note(SimpleNamespace(_accumulated_usage=accumulated), 4000)
    assert 'requested-tier estimate' in note and 'provider retention is unverified' in note


def test_cache_evidence_is_settlement_only_on_journal_export():
    from ouroboros.usage_journal import export_rows
    evidence = {'requested': '1h', 'http': None, 'provider_reported': None}
    row = {'attempt_id': 'cache-export', 'kind': 'attempt', 'state': 'settled',
           'model': 'm', 'provider': 'openai-compatible', 'prompt_cache_ttl_evidence': evidence}
    rows = export_rows(None, [], [(row, 'reserved-time', 'dispatch-time')], {})
    assert [r['state'] for r in rows] == ['reserved', 'dispatched', 'settled']
    assert all('prompt_cache_ttl_evidence' not in r for r in rows[:-1])
    assert rows[-1]['prompt_cache_ttl_evidence'] == evidence


def test_observed_bare_breakpoint_has_no_mapped_requested_horizon():
    state = {'_last_prompt_cache_ttl': '1h', '_last_prompt_cache_ttl_evidence': {
        'http': {'breakpoints': 2, 'key_present': True, 'options_present': True}}}
    assert cache_horizon_note(SimpleNamespace(_accumulated_usage=state), 4000) == ''
    _record_round_cache_facts(state, {'prompt_cache_ttl': '5m'}, round_idx=2)
    assert state['_last_prompt_cache_ttl_evidence'] is None
    assert 'requested-tier estimate' in cache_horizon_note(SimpleNamespace(_accumulated_usage=state), 4000)


def test_attachment_failure_keeps_native_observation_unknown():
    class Unattachable:
        __slots__ = ()
    response = Unattachable()
    with obs.http_observation_scope():
        obs._HTTP_OBSERVATIONS.get().append({'cache': {'breakpoints': 0}})
        assert obs.bind_http_observations(response) is response
    usage = {'prompt_cache_ttl_evidence': {'http': {'breakpoints': 88}}}
    obs.record_cache_ttl_evidence(usage, response, '1h')
    assert usage['prompt_cache_ttl_evidence']['http'] is None


def test_noncompatible_provider_cannot_author_host_cache_evidence():
    from ouroboros._usage_response import usage_from_response
    body = {'id': 'native-control', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
             'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 1,
             'prompt_cache_ttl': '1h', 'prompt_cache_ttl_evidence': {'http': {}}}}
    extracted, _, _ = usage_from_response(body)
    assert 'prompt_cache_ttl_evidence' not in extracted and extracted['prompt_cache_ttl'] == '1h'
    _, usage = LLMClient()._normalize_remote_response(body, {'provider': 'openrouter', 'resolved_model': 'fixture'},
                                                    skip_cost_fetch=True)
    assert 'prompt_cache_ttl_evidence' not in usage and usage['prompt_cache_ttl'] == '1h'


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('requested', [None, '1h'])
@pytest.mark.parametrize('reported', [None, 'provider-tier'])
@pytest.mark.parametrize('cost', [None, .25])
def test_rejected_public_reply_keeps_requested_provider_cache_distinct(
        option_wire, monkeypatch, asynchronous, stream, requested, reported, cost):
    from ouroboros import net_transport as nt, usage_accounting as ua
    invoke, sent, _, root, task = option_wire
    monkeypatch.setattr(config, 'resolve_prompt_cache_ttl', lambda: requested or 'default')
    messages = [{'role': 'system', 'content': [{'type': 'text', 'text': 'Fixture governance',
                 **({'cache_control': {'type': 'ephemeral', 'ttl': requested}} if requested else {})}]},
                {'role': 'user', 'content': 'Rejected fixture'}]
    hm = nt._openai_httpx_module()
    original_handle = hm.HTTPTransport.handle_request
    def handle(self, request):
        body = original_handle(self, request).json()
        body.update(status='failed', error={'code': 'fixture-rejection'})
        body['usage'].update(input_tokens_details={'cached_tokens': 7},
                             prompt_cache_ttl_evidence={'http': {'breakpoints': 99}})
        if reported is not None: body['usage']['prompt_cache_ttl'] = reported
        if cost is not None: body['usage']['cost'] = cost
        return hm.Response(200, json=body)
    async def ahandle(self, request): return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', ahandle)
    public = LLMClient.chat_async if asynchronous else LLMClient.chat
    if asynchronous:
        async def entry(self, **kw):
            kw['messages'] = messages
            return await public(self, **kw)
        monkeypatch.setattr(LLMClient, 'chat_async', entry)
    else:
        def entry(self, **kw):
            kw['messages'] = messages
            return public(self, **kw)
        monkeypatch.setattr(LLMClient, 'chat', entry)
    with pytest.raises(nt.CopilotRejectedResponse):
        invoke(asynchronous=asynchronous, stream=stream)
    assert len(sent) == 1
    from tests._usage_store_testing import ledger_rows
    row = ledger_rows(root)[-1]
    assert row['state'] == 'settled' and row['cached_tokens'] == 7
    assert row['prompt_cache_ttl'] == (requested or '')
    assert row['prompt_cache_ttl_evidence'] == {
        'requested': requested, 'request_boundary': 'sdk_input', 'http': None,
        'provider_reported': reported}
    assert row['cost_usd'] == cost and row['cost_final'] == (cost is not None)
    with ua.usage_scope(ua.UsageScope(task_id=task, category='verification', non_task_operation=True)):
        assert ua.last_task_cache_split(task, row['model'], provider=row['provider']) is None


@pytest.mark.parametrize('provider', ['openai-compatible', 'openrouter'])
def test_abandoned_error_late_settlement_replaces_cache_evidence(tmp_path, provider):
    from ouroboros import usage_accounting as ua
    from ouroboros._usage_wait import late_settler
    from ouroboros._usage_response import usage_from_response
    from ouroboros.copilot_responses import CopilotRejectedResponse
    ua._reset_task_cache_splits()
    request = ua.AttemptRequest(model='vendor/fixture', provider=provider, reservation_usd=1.,
                                drive_root=tmp_path, task_id='late-cache', root_task_id='late-cache',
                                source='offline_mock_fixture', prompt_cache_ttl='1h')
    error = CopilotRejectedResponse({'status': 'failed', 'usage': {
        'input_tokens': 12, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 7},
        'prompt_cache_ttl': 'provider-tier', 'prompt_cache_ttl_evidence': {'requested': 'forged'},
        'cost': .25}}, 'fixture rejection')
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id='late-cache',
                                    non_task_operation=True)):
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        abandoned = RuntimeError('receiver left')
        abandoned.receiver_abandoned = True
        assert ua._terminalize_failed_attempt(reservation, abandoned, request) == 'dispatched'
        # A stale extra field left by an earlier observation must not survive replacement.
        ua._transition(reservation, 'unresolved', reason='observation incomplete',
                       prompt_cache_ttl_evidence={'requested': 'stale'})
        late_settler(reservation, request, usage_from_response, None, None, None)(error=error)
        with hold(tmp_path) as view: row = view.attempt(reservation.attempt_id)
        assert row['state'] == 'settled' and row['cost_usd'] == .25 and row['cost_final']
        assert row['cached_tokens'] == 7
        if provider == 'openai-compatible':
            assert row['prompt_cache_ttl'] == '1h'
            assert row['prompt_cache_ttl_evidence'] == {
                'requested': '1h', 'request_boundary': 'sdk_input', 'http': None,
                'provider_reported': 'provider-tier'}
        else:
            assert row['prompt_cache_ttl'] == 'provider-tier'
            assert 'prompt_cache_ttl_evidence' not in row
        split = ua.last_task_cache_split('late-cache', row['model'], provider=provider)
        assert (split is None) == (provider == 'openai-compatible')


def test_zero_retention_discards_split_even_in_same_monotonic_tick(monkeypatch):
    from ouroboros import _usage_cache_splits as splits
    monkeypatch.setattr(splits.time, 'monotonic', lambda: 10.)
    splits.reset_task_cache_splits()
    splits.stash_task_cache_split('fixed-clock', 'vendor/m', 7, provider='openai-compatible', ttl_seconds=3600.)
    assert splits.last_task_cache_split('fixed-clock', 'vendor/m', provider='openai-compatible') == 7
    splits.stash_task_cache_split('fixed-clock', 'vendor/m', 7, provider='openai-compatible', ttl_seconds=0.)
    assert splits.last_task_cache_split('fixed-clock', 'vendor/m', provider='openai-compatible') is None
    assert not splits._SPLITS
