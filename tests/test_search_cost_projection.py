"""Public search -> real SDK -> offline HTTP -> settlement/event price fidelity.

Tariffs are injected current-shaped fixtures, not direct-provider price discovery
or service bills. The network boundary is MockTransport; no provider call runs.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from ouroboros import config, net_transport, pricing, usage_accounting as ua
from ouroboros.tools import search
from tests._usage_store_testing import ledger_rows


@pytest.fixture
def run_search(monkeypatch):
    sent = []
    monkeypatch.setattr(ua, '_reservation_cost', lambda request: 0.)
    monkeypatch.setattr(search, '_search_key', lambda *args: 'fixture-key')
    monkeypatch.setattr(search, '_resolve_openai_client_settings',
                        lambda *args: ('fixture-key', None, 'openai', 'openai'))
    monkeypatch.setattr('ouroboros.model_slots.resolve_processing_preference', lambda *args, **kwargs: '')

    def run(provider, raw_usage, *, tariff=(1., .1, 1.25, 4.), outer=None, family='gpt'):
        model = 'claude-fixture' if provider == 'anthropic' or family == 'claude' else 'gpt-fixture'
        pricing_model = f'{provider}/{model}'
        route = dict(source=provider, legs=[dict(source=provider, backend={
            'openai': 'openai_responses', 'anthropic': 'anthropic_server_tool',
            'openrouter': 'openrouter_server_tool'}[provider], model=model)], unavailable=[])
        monkeypatch.setattr(search, 'resolve_web_search_route', lambda **kwargs: copy.deepcopy(route))
        monkeypatch.setattr(pricing, 'get_pricing', lambda **kwargs: (
            {pricing_model: pricing.PricingSchedule(tariff, source={'service_tier': 'default'})}
            if tariff is not None else {}))
        native_http = httpx
        if provider == 'anthropic':
            pytest.importorskip('anthropic')
            import anthropic._base_client as base_client
            native_http = __import__('httpx2') if hasattr(base_client, 'httpx2') else httpx
        body = {'usage': copy.deepcopy(raw_usage), **(outer or {})}
        if provider == 'anthropic':
            body.update(id='msg_fixture', type='message', role='assistant', model=model,
                        content=[{'type': 'text', 'text': 'answer'}], stop_reason='end_turn', stop_sequence=None)
        elif provider == 'openrouter':
            body.update(id='chat_fixture', object='chat.completion', created=1, model=model, service_tier='default',
                        choices=[{'index': 0, 'finish_reason': 'stop',
                                  'message': {'role': 'assistant', 'content': 'answer'}}])
        else:
            body.update(id='resp_fixture', object='response', created_at=1, status='completed',
                        model=model, output=[], error=None, incomplete_details=None,
                        parallel_tool_calls=True, tool_choice='auto', tools=[])

        def handle(request):
            sent.append({'provider': provider, 'body': json.loads(request.content)})
            if provider != 'openai':
                return native_http.Response(200, json=body)
            events = [{'type': 'response.output_text.delta', 'delta': 'answer', 'content_index': 0,
                       'item_id': 'message_fixture', 'output_index': 0, 'sequence_number': 1, 'logprobs': []},
                      {'type': 'response.completed', 'response': body, 'sequence_number': 2}]
            wire = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire)

        def client(**kwargs):
            return openai.OpenAI(api_key='fixture-key', base_url='https://example.invalid/v1',
                                 max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        monkeypatch.setattr(net_transport, 'web_search_openai_client', client)
        if provider == 'anthropic':
            anthropic = pytest.importorskip('anthropic')
            native_anthropic = anthropic.Anthropic
            monkeypatch.setattr(anthropic, 'Anthropic', lambda **kwargs: native_anthropic(
                api_key='fixture-key', base_url='https://example.invalid', max_retries=0,
                http_client=native_http.Client(transport=native_http.MockTransport(handle))))
        ctx = SimpleNamespace(task_id='search-price-fixture', pending_events=[], task_metadata={})
        before = len(sent)
        with ua.usage_scope(ua.UsageScope(drive_root=config.DATA_DIR, task_id=ctx.task_id,
                                          non_task_operation=True)):
            result = json.loads(search._web_search(ctx, 'fixture query', model=model))
        assert result.get('answer') == 'answer', result
        assert len(sent) == before + 1
        assert len(ctx.pending_events) == 1
        event = ctx.pending_events[0]
        attempt = event['ledger_attempt_ids'][0]
        row = next(row for row in ledger_rows(config.DATA_DIR) if row['attempt_id'] == attempt)
        assert row['state'] == 'settled'
        if provider == 'anthropic':
            assert row['model'] == f'anthropic/{model}'
            assert sent[-1]['body']['model'] == model
        assert event['accounting_authority'] == 'physical_attempt_ledger'
        return event, row, sent[-1]
    return run


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
@pytest.mark.parametrize('detail,read', [(800, 800), (0, 0), ('bad', 7)])
def test_public_search_prices_normalized_cache_split(run_search, provider, detail, read):
    raw = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': read,
           'cache_creation_input_tokens': 200} if provider == 'anthropic' else {
               'input_tokens' if provider == 'openai' else 'prompt_tokens': 1000,
               'output_tokens' if provider == 'openai' else 'completion_tokens': 50,
               'cached_tokens': 7, 'input_tokens_details': {'cached_tokens': detail, 'cache_write_tokens': 200}}
    event, row, sent = run_search(provider, raw)
    total = 100 + read + 200 if provider == 'anthropic' else 1000
    expected = ((total - read - 200) * 1 + read * .1 + 200 * 1.25 + 50 * 4) / 1_000_000
    if provider != 'openrouter':
        expected = round(expected, 6)  # model-catalog precision; endpoint tariffs retain full precision
    assert event['cost'] == row['cost_usd']
    assert event['cost'] == pytest.approx(expected)
    assert row['prompt_tokens'] == total and row['cached_tokens'] == read and row['cache_write_tokens'] == 200
    assert event['cached_tokens'] == read and event['cache_write_tokens'] == 200
    assert event['cost_estimated'] is True and row['cost_final'] is False
    assert sent['body']['tools']


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
@pytest.mark.parametrize('cash', [0., .25, True, -1., 'bad'])
def test_public_search_reported_cash_wins_invalid_stays_unknown(run_search, provider, cash):
    raw = {'input_tokens': 1000, 'output_tokens': 50, 'cost': cash} if provider != 'openrouter' else {
        'prompt_tokens': 1000, 'completion_tokens': 50, 'cost': cash}
    event, row, _ = run_search(provider, raw)
    expected = cash if cash in (0., .25) and not isinstance(cash, bool) else None
    assert event['cost'] == row['cost_usd'] == expected
    assert event['cost_estimated'] is False
    assert row['cost_final'] is (expected is not None)


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
@pytest.mark.parametrize('tariff', [None, (1., None, 1.25, 4.), (1., .1, None, 4.)])
def test_public_search_missing_tariffs_remain_unknown(run_search, provider, tariff):
    raw = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 700,
           'cache_creation_input_tokens': 200} if provider == 'anthropic' else {
        'prompt_tokens': 1000, 'completion_tokens': 50,
        'prompt_tokens_details': {'cached_tokens': 700, 'cache_write_tokens': 200}}
    event, row, _ = run_search(provider, raw, tariff=tariff)
    assert event['cost'] is row['cost_usd'] is None
    assert event['cost_estimated'] is False and row['cost_final'] is False


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
def test_public_search_total_cost_and_outer_billing_claims_are_distinct(run_search, provider):
    raw = {'input_tokens': 100, 'output_tokens': 50, 'total_cost': .5} if provider != 'openrouter' else {
        'prompt_tokens': 100, 'completion_tokens': 50, 'total_cost': .5}
    claims = {'copilot_usage': {'billing': {'cache_write_tokens': 9999}}}
    event, row, _ = run_search(provider, raw, outer=claims)
    assert event['cost'] == row['cost_usd'] == .5
    assert event['cost_estimated'] is False and row['cost_final'] is True
    assert row['cache_write_tokens'] is None
    expected = {'source': 'provider_response', **claims}
    if provider == 'openai':
        expected['prompt_cache_retention'] = None  # real Responses SDK's nullable native field
    assert event['usage']['provider_usage'] == row['provider_usage'] == expected


def test_openrouter_byok_consumed_cost_is_not_double_added(run_search):
    raw = {'prompt_tokens': 100, 'completion_tokens': 50, 'cost': .1, 'is_byok': True,
           'cost_details': {'upstream_inference_cost': .4}}
    event, row, _ = run_search('openrouter', raw)
    assert event['cost'] == row['cost_usd'] == .5
    assert event['cost_estimated'] is False and row['cost_basis'] == 'byok_gateway_plus_upstream'


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
def test_public_search_absent_usage_is_not_priced_as_measured_zero(run_search, provider):
    event, row, _ = run_search(provider, {})
    assert event['cost'] is row['cost_usd'] is None
    assert event['prompt_tokens'] is event['completion_tokens'] is None
    assert event['cost_estimated'] is False and row['cost_final'] is False


@pytest.mark.parametrize('tokens', [{}, {'prompt_tokens': None, 'completion_tokens': None},
                                   {'prompt_tokens': 0, 'completion_tokens': 0}])
@pytest.mark.parametrize('reported', [None, 0.])
def test_shared_projection_does_not_estimate_unreported_work(monkeypatch, tokens, reported):
    calls = []
    monkeypatch.setattr(pricing, 'estimate_cost_optional', lambda *args, **kwargs: calls.append(kwargs) or 0.)
    usage = {**tokens, 'cost': reported}
    cost, _, _, estimated = pricing.normalize_usage_cost(usage, model='openai/fixture', use_local=False)
    assert cost == reported and estimated is False and calls == []
    local = dict(tokens)
    assert pricing.normalize_usage_cost(local, model='fixture', use_local=True)[0] == 0.
    assert local['cost_final'] is True


@pytest.mark.parametrize('provider', ['openai', 'openrouter', 'anthropic'])
def test_search_price_identity_is_host_owned(monkeypatch, provider):
    seen = []
    monkeypatch.setattr(pricing, 'estimate_cost_optional', lambda *args, **kwargs: seen.append((args, kwargs)) or .1)
    usage = {'prompt_tokens': 10, 'completion_tokens': 1, 'provider': 'local',
             'resolved_model': 'unrelated-model', 'cached_tokens': 7}
    original = copy.deepcopy(usage)
    ctx = SimpleNamespace(pending_events=[], task_id='fixture')
    search._emit_simple_usage(ctx, provider=provider, model='selected', usage=usage)
    assert seen[0][0][0] == f'{provider}/selected' and seen[0][1]['provider'] == provider
    assert seen[0][1]['cache_usage']['cached_tokens'] == 7
    assert ctx.pending_events[0]['cost'] == .1 and ctx.pending_events[0]['cost_estimated']
    assert usage == original and ctx.pending_events[0]['provider'] == provider


def test_anthropic_partial_diagnostic_total_is_not_billing_total(monkeypatch):
    from ouroboros._usage_response import usage_from_response
    from ouroboros.event_usage import anthropic_token_counts
    raw = {'input_tokens': 10, 'output_tokens': 1, 'cache_read_input_tokens': 30}
    usage, _, _ = usage_from_response({'usage': raw})
    usage['reported_tokens'] = anthropic_token_counts(raw)
    monkeypatch.setattr(pricing, 'get_pricing', lambda **kwargs: {'anthropic/fixture': (1., .1, 1.25, 4.)})
    ctx = SimpleNamespace(pending_events=[], task_id='fixture')
    search._emit_simple_usage(ctx, provider='anthropic', model='fixture', usage=usage)
    event = ctx.pending_events[0]
    assert usage['prompt_tokens'] == 40 and event['prompt_tokens'] is None
    assert event['cached_tokens'] == 30 and event['cache_write_tokens'] is None
    assert event['cost'] == .000017 and event['cost_estimated']


@pytest.mark.parametrize('mode,known', [('default', True), ('unknown', False)])
def test_search_prices_mixed_write_tiers_and_observed_processing(monkeypatch, mode, known):
    row = pricing.PricingSchedule((1., .1, 1.25, 4.), cache_write_1h=2., source={'service_tier': 'default'})
    monkeypatch.setattr(pricing, 'get_pricing', lambda **kwargs: {'openrouter/fixture': row})
    usage = {'prompt_tokens': 60, 'completion_tokens': 1, 'cached_tokens': 30,
             'cache_write_tokens': 20, 'prompt_cache_ttl': '1h',
             'cache_write_tokens_by_ttl': {'5m': 15, '1h': 5},
             'processing': {'observed': 'standard' if known else 'unknown', 'observedNative': [mode]}}
    ctx = SimpleNamespace(pending_events=[], task_id='fixture')
    search._emit_simple_usage(ctx, provider='openrouter', model='fixture', usage=usage)
    assert ctx.pending_events[0]['cost'] == (pytest.approx(.00004575) if known else None)
    assert ctx.pending_events[0]['cost_estimated'] is known


@pytest.mark.parametrize('family', ['gpt', 'claude'])
@pytest.mark.parametrize('cached,written', [(800, 0), (400, 500)])
def test_public_openrouter_search_both_model_families(run_search, family, cached, written):
    usage = {'prompt_tokens': 1000, 'completion_tokens': 50,
             'prompt_tokens_details': {'cached_tokens': cached, 'cache_write_tokens': written}}
    event, row, sent = run_search('openrouter', usage, family=family)
    expected = ((1000 - cached - written) + cached * .1 + written * 1.25 + 50 * 4) / 1_000_000
    assert event['cost'] == row['cost_usd'] == pytest.approx(expected)
    assert event['cost_estimated'] and not row['cost_final']
    assert sent['body']['model'] == f'{family}-fixture'


@pytest.mark.parametrize('cash,estimated,expected', [(None, True, .0012), (.25, False, .25)])
def test_search_estimate_flag_survives_actual_supervisor_projection(run_search, monkeypatch, cash, estimated, expected):
    from supervisor import events_budget
    raw = {'prompt_tokens': 1000, 'completion_tokens': 50}
    if cash is not None:
        raw['cost'] = cash
    event, row, _ = run_search('openrouter', raw)
    published = []
    monkeypatch.setattr(events_budget, 'append_jsonl', lambda *args, **kwargs: published.append(args[1]))
    events_budget._handle_llm_usage(event, SimpleNamespace(DRIVE_ROOT=config.DATA_DIR))
    stored = next(item for item in published if item['type'] == 'llm_usage')
    assert stored['cost'] == row['cost_usd'] == pytest.approx(expected)
    assert stored['cost_estimated'] is estimated and stored['cost_final'] is (not estimated)
    assert stored['ledger_attempt_ids'] == event['ledger_attempt_ids']


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
def test_search_reported_price_flags_are_host_derived(run_search, provider):
    raw = {'prompt_tokens': 100, 'completion_tokens': 50, 'cost': .25,
           'cost_estimated': True, 'cost_final': False}
    event, row, _ = run_search(provider, raw)
    assert event['cost'] == row['cost_usd'] == .25 and row['cost_final']
    assert event['cost_estimated'] is False and event['usage']['cost_final'] is True


def test_unknown_cash_cannot_keep_a_provider_estimate_flag(monkeypatch):
    monkeypatch.setattr(pricing, 'get_pricing', lambda **kwargs: {})
    usage = {'cost_estimated': True}
    cost, _, _, estimated = pricing.normalize_usage_cost(usage, model='openai/fixture', use_local=False)
    assert cost is None and estimated is False and usage['cost_final'] is False


def test_native_search_fixture_skips_before_client_construction_without_sdk(run_search, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, 'anthropic', None)
    with pytest.raises(pytest.skip.Exception):
        run_search('anthropic', {'input_tokens': 10, 'output_tokens': 1})


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'openrouter'])
@pytest.mark.parametrize('cash', [None, 'bad'])
def test_search_unknown_cash_flag_survives_supervisor_without_reviving_estimate(run_search, monkeypatch, provider, cash):
    from supervisor import events_budget
    raw = {'prompt_tokens': 10, 'completion_tokens': 1, 'cost': cash, 'cost_estimated': True}
    event, row, _ = run_search(provider, raw, tariff=None)
    published = []
    monkeypatch.setattr(events_budget, 'append_jsonl', lambda *args, **kwargs: published.append(args[1]))
    events_budget._handle_llm_usage(event, SimpleNamespace(DRIVE_ROOT=config.DATA_DIR))
    stored = next(item for item in published if item['type'] == 'llm_usage')
    assert stored['cost'] is row['cost_usd'] is None
    assert stored['cost_estimated'] is False and stored['cost_final'] is False


@pytest.mark.parametrize('details', [{}, {'upstream_inference_cost': 'bad'}])
def test_search_incomplete_byok_cash_is_not_complete_gateway_price(run_search, monkeypatch, details):
    from supervisor import events_budget
    event, row, _ = run_search('openrouter', {'prompt_tokens': 10, 'completion_tokens': 1,
        'cost': .1, 'is_byok': True, 'cost_details': details}, tariff=None)
    published = []
    monkeypatch.setattr(events_budget, 'append_jsonl', lambda *args, **kwargs: published.append(args[1]))
    events_budget._handle_llm_usage(event, SimpleNamespace(DRIVE_ROOT=config.DATA_DIR))
    stored = next(item for item in published if item['type'] == 'llm_usage')
    assert event['cost'] is row['cost_usd'] is stored['cost'] is None
    assert stored['cost_final'] is False and stored['cost_estimated'] is False
