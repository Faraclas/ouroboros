"""Native billing/retention claims are evidence, not standard token or money facts.

Real public clients/SDK/ledger; HTTP is mocked. No service retention is measured.
"""
import asyncio
import copy
import json
import socket
import uuid
from types import SimpleNamespace

import pytest

from ouroboros import config, net_transport as nt, pricing, usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros import request_wire_contract as wire
from ouroboros import request_wire_recovery as recovery
from ouroboros.usage_store import hold


NATIVE_USAGE = {'token_details': [
    {'token_type': 'input', 'token_count': 25, 'model': 'fixture'},
    {'token_type': 'cache_read', 'token_count': 0, 'model': 'fixture'},
    {'token_type': 'cache_write', 'token_count': 539, 'model': 'fixture'},
    {'token_type': 'output', 'token_count': 26, 'model': 'fixture'},
], 'total_nano_aiu': 331500000}


def evidence(body):
    fields = {key: copy.deepcopy(body[key]) for key in ('copilot_usage', 'prompt_cache_retention') if key in body}
    if isinstance(body.get('reasoning'), dict) and 'effort' in body['reasoning']:
        fields['reasoning_effort'] = body['reasoning']['effort']
    return {'source': 'provider_response', **fields} if fields else None


@pytest.fixture
def native_wire(monkeypatch, tmp_path):
    hm = nt._openai_httpx_module()
    sent, state = [], {}
    def no_network(*args, **kwargs):
        pytest.fail('unexpected socket in native-usage mock')
    def target(self, model):
        name = model.removeprefix('openai-compatible::')
        assert name in {'gpt-6.1-sol', 'claude-opus-5.5'}
        return dict(provider='openai-compatible', resolved_model=name,
                    usage_model='openai-compatible/' + name, api_key='fixture',
                    base_url='https://api.business.githubcopilot.com', supports_openrouter_extensions=False)
    def handle(self, request):
        payload = json.loads(request.read())
        sent.append(payload)
        body = copy.deepcopy(state['body'])
        if payload.get('stream'):
            # Native Claude uses Chat SSE; GPT Responses is buffered by the existing codec.
            chunk = {**body, 'object': 'chat.completion.chunk', 'choices': [
                {'index': 0, 'delta': body['choices'][0]['message'], 'finish_reason': 'stop'}]}
            return hm.Response(200, content=('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode(),
                               headers={'content-type': 'text/event-stream'})
        return hm.Response(200, json=body)
    async def ahandle(self, request): return handle(self, request)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_network)
    monkeypatch.setattr(LLMClient, '_resolve_remote_target', target)
    monkeypatch.setattr(ua, '_reservation_cost', lambda request: 0.)
    monkeypatch.setattr(pricing, 'estimate_cost_optional', lambda *a, **k: None)
    monkeypatch.setattr(wire, 'canonical_wire_evidence_root', lambda: tmp_path)
    original_observe = recovery.observe_wire_semantics
    def observe(**kw):
        assert kw['candidate'].source_profile.model == 'claude-opus-5.5', 'translated GPT must not teach Chat compatibility'
        return original_observe(**kw)
    monkeypatch.setattr(recovery, 'observe_wire_semantics', observe)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', ahandle)
    root, task = config.DATA_DIR, 'native-usage-' + uuid.uuid4().hex
    def invoke(model, body, asynchronous=False, stream=False):
        state['body'] = body
        client = LLMClient()
        call = dict(model='openai-compatible::' + model, messages=[{'role': 'user', 'content': 'Fixture'}],
                    max_tokens=1024, stream=stream, wait_for_resources=False)
        scope = ua.UsageScope(drive_root=root, task_id=task, category='verification', non_task_operation=True)
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            if asynchronous:
                async def run():
                    try: return await client.chat_async(**call)
                    finally:
                        for remote in client._async_remote_clients.values(): await remote.close()
                return asyncio.run(run())
            try: return client.chat(**call)
            finally:
                for remote in client._remote_clients.values(): remote.close()
    return invoke, sent, root, task


def response(model, standard_write=None):
    body = {'id': 'native-fixture', 'model': model, 'copilot_usage': copy.deepcopy(NATIVE_USAGE),
            'prompt_cache_retention': '24h', 'reasoning': {'effort': 'high'},
            'usage': {'prompt_tokens': 564, 'completion_tokens': 26,
                      'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': standard_write}}}
    if model.startswith('gpt'):
        body.update(status='completed', output=[{'type': 'message', 'role': 'assistant', 'status': 'completed',
                          'content': [{'type': 'output_text', 'text': 'ok'}]}])
        body['usage'] = {'input_tokens': 564, 'output_tokens': 26,
                         'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': standard_write}}
    else:
        body['choices'] = [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]
    return body


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('standard_write', [None, 0, 7])
def test_native_claims_reach_both_usage_consumers_without_reinterpreting_counts(
        native_wire, model, asynchronous, stream, standard_write):
    invoke, sent, root, task = native_wire
    body = response(model, standard_write)
    before = copy.deepcopy(body)
    message, usage = invoke(model, body, asynchronous, stream)
    assert message['content'] == 'ok' and len(sent) == 1 and body == before
    expected = evidence(body)
    assert usage.get('provider_usage') == expected
    assert usage.get('cache_write_tokens') == standard_write
    assert usage['cost'] is None and not usage['cost_final']
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['state'] == 'settled' and row.get('provider_usage') == expected
    assert row['cache_write_tokens'] == standard_write and row['cost_usd'] is None
    assert row['prompt_tokens'] == 564  # cache write539 is already included; do not add it again
    from ouroboros.event_usage import event_token_counts
    assert event_token_counts(usage)['cache_write_tokens'] == standard_write
    from ouroboros.loop_llm_call import _record_round_cache_facts
    from ouroboros.tools.control_task_results import cache_horizon_note
    accumulated = {}
    _record_round_cache_facts(accumulated, usage, round_idx=1)
    assert accumulated['_last_round_cache_usage']['provider_usage'] == expected
    assert cache_horizon_note(SimpleNamespace(_accumulated_usage=accumulated), 4000) == ''
    assert ua.last_task_cache_split(task, row['model'], provider='openai-compatible') is None


def test_settlement_claims_do_not_backdate_or_survive_absent_replacement(tmp_path):
    from ouroboros.usage_journal import export_rows
    request = ua.AttemptRequest(model='fixture', provider='openai-compatible', reservation_usd=1.,
                                drive_root=tmp_path, task_id='native-late', root_task_id='native-late')
    scope = ua.UsageScope(drive_root=tmp_path, task_id='native-late', non_task_operation=True)
    with ua.usage_scope(scope):
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        ua._transition(reservation, 'unresolved', reason='old observation', provider_usage={'stale': True})
        ua.settle_attempt(reservation, {'prompt_tokens': 564, 'completion_tokens': 26})
        with hold(tmp_path) as view: row = view.attempt(reservation.attempt_id)
    assert 'provider_usage' not in row
    value = evidence(response('claude-opus-5.5'))
    row['provider_usage'] = value
    exported = export_rows(None, [], [(row, 'reserved-time', 'dispatch-time')], {})
    assert [r['state'] for r in exported] == ['reserved', 'dispatched', 'settled']
    assert all('provider_usage' not in r for r in exported[:-1])
    assert exported[-1]['provider_usage'] == value


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('cost', [None, .25])
def test_rejected_claude_sdk_stream_keeps_outer_claims(native_wire, monkeypatch, asynchronous, cost):
    invoke, sent, root, task = native_wire
    hm = nt._openai_httpx_module()
    body = response('claude-opus-5.5')
    if cost is not None: body['usage']['cost'] = cost
    def handle(self, request):
        sent.append(json.loads(request.read()))
        # A malformed completed tool is rejected by the real stream assembler.
        chunk = {**body, 'object': 'chat.completion.chunk', 'choices': [{'index': 0,
            'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                      'function': {'name': '', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}
        return hm.Response(200, content=('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode(),
                           headers={'content-type': 'text/event-stream'})
    async def ahandle(self, request): return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', ahandle)
    from ouroboros.llm_stream import RejectedProviderStream
    with pytest.raises(RejectedProviderStream): invoke('claude-opus-5.5', body, asynchronous, True)
    assert len(sent) == 1
    from tests._usage_store_testing import ledger_rows
    row = next(r for r in ledger_rows(root) if r.get('task_id') == task)
    assert row['state'] == 'settled' and row.get('provider_usage') == evidence(body)
    assert row['prompt_tokens'] == 564 and row['cache_write_tokens'] is None
    assert row['cost_usd'] == cost and row['cost_final'] == (cost is not None)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('cost', [None, .25])
def test_rejected_gpt_public_reply_preserves_claims_not_extra_counters(native_wire, asynchronous, stream, cost):
    invoke, sent, root, task = native_wire
    body = response('gpt-6.1-sol')
    body.update(status='failed', error={'code': 'fixture'})
    if cost is not None: body['usage']['cost'] = cost
    with pytest.raises(nt.CopilotRejectedResponse): invoke('gpt-6.1-sol', body, asynchronous, stream)
    from tests._usage_store_testing import ledger_rows
    row = next(r for r in ledger_rows(root) if r.get('task_id') == task)
    assert len(sent) == 1 and row['state'] == 'settled'
    assert row['provider_usage'] == evidence(body) and row['prompt_tokens'] == 564
    assert row['cache_write_tokens'] is None and row['cost_usd'] == cost
    assert row['cost_final'] == (cost is not None)


@pytest.mark.parametrize('error_reply', [False, True])
@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
def test_abandoned_sender_late_settlement_uses_same_attribution(tmp_path, model, error_reply):
    from ouroboros._usage_response import usage_from_response
    from ouroboros._usage_wait import late_settler
    body = response(model)
    body['usage']['cost'] = .25
    request = ua.AttemptRequest(model=model, provider='openai-compatible', reservation_usd=1.,
                                drive_root=tmp_path, task_id='native-late', root_task_id='native-late')
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id='native-late', non_task_operation=True)):
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        abandoned = RuntimeError('receiver left')
        abandoned.receiver_abandoned = True
        assert ua._terminalize_failed_attempt(reservation, abandoned, request) == 'dispatched'
        ua._transition(reservation, 'unresolved', provider_usage={'stale': True}, reason='incomplete observation')
        settle = late_settler(reservation, request, usage_from_response, None, None, None)
        if error_reply: settle(error=nt.CopilotRejectedResponse(body, 'fixture'))
        else: settle(result=body)
        with hold(tmp_path) as view: row = view.attempt(reservation.attempt_id)
    assert row['state'] == 'settled' and row['provider_usage'] == evidence(body)
    assert row['prompt_tokens'] == 564 and row['cache_write_tokens'] is None
    assert row['cost_usd'] == .25 and row['cost_final']


@pytest.mark.parametrize('native', [None, {}, {'token_details': [{'token_count': -1}]}, 'malformed'])
def test_partial_provider_claims_are_kept_without_adopting_values_or_inner_provenance(native):
    from ouroboros._usage_response import usage_from_response
    body = response('claude-opus-5.5')
    body['copilot_usage'] = native
    body['usage']['provider_usage'] = {'source': 'trusted_http', 'cache_write_tokens': 999, 'cost': 5}
    expected = evidence(body)
    extracted, cost, final = usage_from_response(body)
    _, usage = LLMClient()._normalize_remote_response(body, {'provider': 'openrouter', 'resolved_model': 'fixture'},
                                                    skip_cost_fetch=True)
    assert extracted['provider_usage'] == usage['provider_usage'] == expected
    assert cost is None and not final and usage['cost'] is None
    for key in ('copilot_usage', 'prompt_cache_retention', 'reasoning'): body.pop(key)
    extracted, _, _ = usage_from_response(body)
    _, usage = LLMClient()._normalize_remote_response(body, {'provider': 'openrouter', 'resolved_model': 'fixture'},
                                                    skip_cost_fetch=True)
    assert 'provider_usage' not in extracted and 'provider_usage' not in usage


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
def test_public_sdk_claim_reaches_actual_round_event_and_last_round(native_wire, tmp_path, model):
    import queue
    from ouroboros.loop_llm_call import call_llm_with_retry
    invoke, sent, root, task = native_wire
    body, accumulated, events = response(model), {}, queue.Queue()
    class FixtureEntry:
        def chat(self, **kw):
            assert kw['stream'] is True
            return invoke(model, body, stream=True)
    answer, _ = call_llm_with_retry(FixtureEntry(), [{'role': 'user', 'content': 'Fixture'}], model,
                                    None, 'medium', 1, tmp_path, task, 1, events, accumulated)
    assert answer['content'] == 'ok' and len(sent) == 1
    rows = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    row = next(r for r in rows if r['type'] == 'llm_round')
    live = next(e['data'] for e in events.queue if e.get('data', {}).get('type') == 'llm_round_finished')
    assert row['provider_usage'] == live['provider_usage'] == accumulated['_last_round_cache_usage']['provider_usage'] == evidence(body)
    assert row['cache_write_tokens'] is None


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_unknown_response_keeps_claims_without_settling_or_resending(native_wire, monkeypatch, model, asynchronous):
    invoke, sent, root, task = native_wire
    body = response(model)
    body.pop('usage')
    hm = nt._openai_httpx_module()
    if model.startswith('gpt'):
        body['status'] = 'in_progress'
    else:
        def handle(self, request):
            sent.append(json.loads(request.read()))
            chunk = {**body, 'object': 'chat.completion.chunk', 'choices': [{'index': 0,
                      'delta': {'role': 'assistant', 'content': 'partial'}, 'finish_reason': None}]}
            return hm.Response(200, content=('data: ' + json.dumps(chunk) + '\n\n').encode(),
                               headers={'content-type': 'text/event-stream'})
        async def ahandle(self, request): return handle(self, request)
        monkeypatch.setattr(hm.HTTPTransport, 'handle_request', handle)
        monkeypatch.setattr(hm.AsyncHTTPTransport, 'handle_async_request', ahandle)
    from ouroboros.llm_stream import IncompleteProviderStream
    with pytest.raises(IncompleteProviderStream): invoke(model, body, asynchronous, True)
    from tests._usage_store_testing import ledger_rows
    row = next(r for r in ledger_rows(root) if r.get('task_id') == task)
    assert len(sent) == 1 and row['state'] == 'unresolved'
    assert row.get('provider_usage') == evidence(body) and row.get('cost_usd') is None
    assert row.get('prompt_tokens') is None and row.get('cache_write_tokens') is None


def test_real_settlement_journal_import_and_reopened_store_preserve_claims(tmp_path):
    from ouroboros._usage_response import usage_from_response
    from ouroboros import usage_store
    from ouroboros.usage_journal import export_rows
    from tests._usage_store_testing import write_journal
    root, restored = tmp_path / 'original', tmp_path / 'restored'
    request = ua.AttemptRequest(model='fixture', provider='openai-compatible', reservation_usd=1.,
                                drive_root=root, task_id='native-export', root_task_id='native-export')
    body = response('claude-opus-5.5')
    body['usage']['cost'] = .25
    extracted, cost, final = usage_from_response(body)
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id='native-export', non_task_operation=True)):
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        ua.settle_attempt(reservation, extracted, cost_usd=cost, cost_final=final)
    with hold(root) as view: row = view.attempt(reservation.attempt_id)
    rows = export_rows(None, [], [(row, row['ts'], row['ts'])], {})
    assert all('provider_usage' not in r for r in rows[:-1])
    write_journal(restored, rows)
    assert usage_store.migrate_from_journal(restored)['status'] == 'completed'
    from ouroboros.store import close_connections_under
    close_connections_under(restored)
    with hold(restored) as view: imported = view.attempt(reservation.attempt_id)
    assert imported['provider_usage'] == evidence(body) and imported['cost_usd'] == .25
    assert imported['prompt_tokens'] == 564 and imported['cache_write_tokens'] is None
