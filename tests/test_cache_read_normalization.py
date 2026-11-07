"""Cache aliases keep return/settlement fidelity; raw diagnostics stay strict.

Public compatible GPT/Claude clients use the real SDK with mocked HTTP, never
provider inference. Synthetic tariff assertions are not actual price claims.
"""
import copy
import json
import queue

import pytest

from ouroboros._usage_response import usage_from_response
from ouroboros.event_usage import event_token_counts
from ouroboros.llm import LLMClient
from ouroboros.llm_pricing import add_usage
from ouroboros.usage_store import hold
from tests.test_copilot_native_usage import evidence, native_wire, response  # noqa: F401


CASES = [
    ({}, None),
    ({'cached_tokens': 0}, 0),
    ({'cached_tokens': 7}, 7),
    ({'cache_read_input_tokens': 0, 'cached_tokens': 7}, 0),
    ({'precached_prompt_tokens': 0}, 0),
    ({'cached_tokens': True}, None),
    ({'cached_tokens': 'bad'}, None),
    ({'cached_tokens': -2}, 0),
    ({'cached_tokens': '3'}, 3),
    ({'cached_tokens': 2.5}, 2),  # existing accounting grammar, not raw diagnostic grammar
    ({'prompt_tokens_details': {'cached_tokens': 0}}, 0),
    ({'input_tokens_details': {'cached_tokens': 0}}, 0),
    ({'cached_tokens': 7, 'prompt_tokens_details': {'cached_tokens': 0}}, 0),
    ({'cached_tokens': 0, 'prompt_tokens_details': {'cached_tokens': 7}}, 7),
    ({'cached_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 'bad'}}, 5),
    ({'prompt_tokens_details': {'cached_tokens': 'bad'}}, None),
    ({'prompt_tokens_details': {'cached_tokens': True}}, None),
    ({'prompt_cache_hit_tokens': 7}, 7),
    ({'prompt_cache_hit_tokens': 0}, 0),
    ({'cached_tokens': 0, 'prompt_cache_hit_tokens': 7}, 0),
    ({'cached_tokens': 'bad', 'prompt_cache_hit_tokens': 7}, None),
    ({'prompt_cache_hit_tokens': 7, 'prompt_tokens_details': {'cached_tokens': 0}}, 0),
    ({'prompt_cache_hit_tokens': 7, 'prompt_tokens_details': {'cached_tokens': 'bad'}}, 7),
    ({'cached_tokens': None, 'prompt_cache_hit_tokens': 7}, 7),
    ({'prompt_tokens_details': 'bad', 'cached_tokens': 0}, 0),
]


@pytest.mark.parametrize('provider', ['openai-compatible', 'deepseek', 'openrouter'])
@pytest.mark.parametrize('raw,expected', CASES)
def test_returned_read_projection_agrees_with_existing_accounting_grammar(raw, expected, provider):
    body = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
            'usage': copy.deepcopy(raw)}
    before = copy.deepcopy(body)
    extracted, _, _ = usage_from_response(body)
    assert extracted['cached_tokens'] == expected
    _, returned = LLMClient(api_key='fixture')._normalize_remote_response(
        body, {'provider': provider, 'resolved_model': 'fixture'}, skip_cost_fetch=True)
    assert returned.get('cached_tokens') == expected
    assert event_token_counts(returned)['cached_tokens'] == expected
    assert body == before


RAW_CASES = [
    ({'cached_tokens': 7, 'prompt_tokens_details': {'cached_tokens': 0}}, 'cached_tokens', 7),
    ({'cached_tokens': 7, 'input_tokens_details': {'cached_tokens': 0}}, 'cached_tokens', 7),
    ({'precached_prompt_tokens': 0}, 'cached_tokens', 0),
    ({'cached_tokens': 'bad', 'prompt_tokens_details': {'cached_tokens': 4}}, 'cached_tokens', 4),
    ({'cache_write_tokens': 7, 'prompt_tokens_details': {'cache_write_tokens': 0}}, 'cache_write_tokens', 7),
    ({'cache_creation_input_tokens': 7, 'input_tokens_details': {'cache_write_tokens': 0}}, 'cache_write_tokens', 7),
    ({'input_tokens_details': {'cache_creation_tokens': 0}}, 'cache_write_tokens', 0),
    ({'input_tokens_details': {'cache_creation_input_tokens': 0}}, 'cache_write_tokens', 0),
    ({'cached_tokens': 2.5}, 'cached_tokens', None),
    ({'cached_tokens': -2}, 'cached_tokens', None),
    ({'cached_tokens': True}, 'cached_tokens', None),
    ({'cache_write_tokens': 2.5}, 'cache_write_tokens', None),
    ({'cache_write_tokens': 5, 'input_tokens_details': {'cache_write_tokens': True}}, 'cache_write_tokens', 5),
    ({'cache_write_tokens': 5, 'input_tokens_details': {'cache_write_tokens': 'bad'}}, 'cache_write_tokens', 5),
    ({'cached_tokens': 0, 'prompt_cache_hit_tokens': 7}, 'cached_tokens', 0),
    ({'cached_tokens': True, 'prompt_cache_hit_tokens': 7}, 'cached_tokens', 7),
]


@pytest.mark.parametrize('raw,key,expected', RAW_CASES)
def test_raw_event_aliases_keep_top_first_and_strict_usable_counts(raw, key, expected):
    from ouroboros.event_token_totals import event_token_totals
    from ouroboros.pricing import emit_llm_usage_event
    from supervisor.events_budget import _handle_llm_usage
    from types import SimpleNamespace
    import tempfile
    from pathlib import Path
    before = copy.deepcopy(raw)
    assert event_token_counts(raw)[key] == expected
    events = queue.Queue()
    emit_llm_usage_event(events, 'fixture', 'fixture', raw, None)
    event = events.get_nowait()
    assert event[key] == expected
    # Temporary standalone producer data, not the live supervisor's records.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _handle_llm_usage(event, SimpleNamespace(DRIVE_ROOT=root))
        rows = [json.loads(line) for line in (root / 'logs/events.jsonl').read_text(encoding='utf-8').splitlines()]
    row = next(r for r in rows if r['type'] == 'llm_usage')
    assert row[key] == expected and row['record_contract_version'] == 2
    summary = event_token_totals([row])[key]
    assert summary['total'] == expected and summary['unknown_count'] == int(expected is None)
    assert raw == before


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('case,expected', [('detail_zero', 0), ('native_zero', 0), ('mirror', 7),
                                         ('mirror_detail_zero', 0), ('unknown', None), ('coerced', 2)])
def test_public_read_usage_ledger_round_and_total_agree(native_wire, model, asynchronous, stream, case, expected):
    invoke, sent, root, task = native_wire
    body = response(model)
    raw = body['usage']
    details = raw['input_tokens_details' if model.startswith('gpt') else 'prompt_tokens_details']
    if case == 'detail_zero': raw['cached_tokens'] = 7
    if case == 'native_zero':
        details.pop('cached_tokens')
        raw.update(cache_read_input_tokens=0, cached_tokens=7)
    if case in {'mirror', 'mirror_detail_zero'}:
        if case == 'mirror': details.pop('cached_tokens')
        raw['prompt_cache_hit_tokens'] = 7
    if case == 'unknown':
        details.pop('cached_tokens')
        raw['cached_tokens'] = 'bad'
    if case == 'coerced': details['cached_tokens'] = 2.5
    _, usage = invoke(model, body, asynchronous, stream)
    assert len(sent) == 1 and usage.get('cached_tokens') == expected
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['state'] == 'settled' and row['cached_tokens'] == expected
    assert usage.get('provider_usage') == row['provider_usage'] == evidence(body)
    assert usage['cost'] is None and row['cost_usd'] is None
    assert event_token_counts(usage)['cached_tokens'] == expected
    from ouroboros.loop_llm_call import _record_round_cache_facts
    accumulated = {}
    _record_round_cache_facts(accumulated, usage, round_idx=1)
    assert accumulated['_last_round_cache_usage']['reported_tokens']['cached_tokens'] == expected
    total = {}
    add_usage(total, usage)
    assert total['reported_tokens']['cached_tokens'] == expected
    assert total['cached_tokens'] == (expected or 0)


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
def test_read_estimation_consumers_receive_same_split(native_wire, monkeypatch, model, asynchronous, stream):
    from ouroboros import pricing, usage_accounting as ua
    seen = []
    def tariff(model, prompt, completion, *, cache_usage, **kw):
        seen.append(copy.deepcopy(cache_usage))
        return .01 + cache_usage['cached_tokens'] * .001
    monkeypatch.setattr(pricing, 'estimate_cost_optional', tariff)
    monkeypatch.setattr(ua, 'estimate_cost_optional', tariff)
    invoke, sent, root, task = native_wire
    body = response(model)
    body['usage']['cached_tokens'] = 7
    _, usage = invoke(model, body, asynchronous, stream)
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert len(sent) == 1 and len(seen) == 2
    assert [r['cached_tokens'] for r in seen] == [0, 0]
    assert usage['cost'] == row['cost_usd'] == .01
    assert usage['cost_final'] is False and row['cost_final'] is False


@pytest.mark.parametrize('raw,expected', [
    ({'prompt_tokens_details': {'cache_write_tokens': True, 'cache_creation_tokens': 7}}, None),
    ({'input_tokens_details': {'cache_write_tokens': True, 'cache_creation_tokens': 7}}, None),
    ({'cached_tokens': 'bad', 'prompt_cache_hit_tokens': 7}, None),
])
def test_shared_normalized_unknown_cannot_revive_lower_alias_in_search_events(tmp_path, raw, expected):
    from ouroboros.tools.search import _emit_simple_usage
    from types import SimpleNamespace
    key = 'cached_tokens' if 'cached_tokens' in raw else 'cache_write_tokens'
    normalized, cost, final = usage_from_response({'usage': raw})
    assert normalized[key] == expected
    ctx = SimpleNamespace(task_id='search-fixture', pending_events=[])
    _emit_simple_usage(ctx, provider='openai', model='fixture', usage=normalized)
    assert ctx.pending_events[0][key] == expected


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('raw,expected', [({'prompt_cache_hit_tokens': 7}, 7),
    ({'prompt_cache_hit_tokens': 0}, 0),
    ({'prompt_cache_hit_tokens': 7, 'prompt_tokens_details': {'cached_tokens': 0}}, 0)])
def test_public_deepseek_mirror_reaches_physical_ledger(native_wire, monkeypatch, asynchronous, stream, raw, expected):
    from ouroboros import request_wire_recovery as recovery
    def target(self, model):
        return dict(provider='deepseek', resolved_model='deepseek-fixture', usage_model='deepseek/deepseek-fixture',
                    api_key='fixture', base_url='https://api.deepseek.com/v1', supports_openrouter_extensions=False,
                    requires_reasoning_echo=True)
    monkeypatch.setattr(LLMClient, '_resolve_remote_target', target)
    monkeypatch.setattr(recovery, 'observe_wire_semantics', lambda **kw: None)
    invoke, sent, root, task = native_wire
    body = response('claude-opus-5.5')
    body['usage'] = {'prompt_tokens': 446, 'completion_tokens': 19, 'cost': .25, **copy.deepcopy(raw)}
    _, usage = invoke('claude-opus-5.5', body, asynchronous, stream)
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert len(sent) == 1 and usage['provider'] == row['provider'] == 'deepseek'
    assert usage['cached_tokens'] == row['cached_tokens'] == expected
    assert usage['prompt_tokens'] == row['prompt_tokens'] == 446
    assert usage['cost'] == row['cost_usd'] == .25 and row['cost_final'] is True


def test_native_prompt_arithmetic_and_block_selection_are_not_changed():
    normalized, cost, final = usage_from_response({'usage': {'input_tokens': 5, 'output_tokens': 2,
        'cache_read_input_tokens': 7, 'cache_creation_input_tokens': 3, 'cost': .25}})
    assert normalized['prompt_tokens'] == 15 and normalized['cached_tokens'] == 7
    assert normalized['cache_write_tokens'] == 3 and (cost, final) == (.25, True)
    normalized, _, _ = usage_from_response({'usage': {'cached_tokens': 5,
        'prompt_tokens_details': 'bad', 'input_tokens_details': {'cached_tokens': 7}}})
    assert normalized['cached_tokens'] == 5  # preserved whole-block selection, not merging


def test_provider_cannot_supply_shared_normalized_cache_fact():
    raw = {'cached_tokens': 'bad', 'prompt_cache_hit_tokens': 7,
           'reported_tokens': dict.fromkeys(('prompt_tokens','completion_tokens','cached_tokens','cache_write_tokens'), 999)}
    normalized, _, _ = usage_from_response({'usage': raw})
    assert event_token_counts(normalized)['cached_tokens'] is None


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('error_reply', [False, True])
@pytest.mark.parametrize('detail,expected', [(0, 0), ('bad', 7), (2.5, 2)])
def test_late_cache_receipt_replaces_unknown_and_survives_journal(tmp_path, model, error_reply, detail, expected):
    from ouroboros import net_transport as nt, usage_accounting as ua, usage_store
    from ouroboros._usage_wait import late_settler
    from ouroboros.usage_journal import export_rows
    from tests._usage_store_testing import write_journal
    root, restored = tmp_path / 'original', tmp_path / 'restored'
    body = response(model)
    body['usage']['cost'] = .25
    body['usage']['cached_tokens'] = 7
    body['usage']['input_tokens_details' if model.startswith('gpt') else 'prompt_tokens_details']['cached_tokens'] = detail
    request = ua.AttemptRequest(model=model, provider='openai-compatible', reservation_usd=1., drive_root=root,
                                task_id='cache-late', root_task_id='cache-late')
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id='cache-late', non_task_operation=True)):
        reservation = ua.reserve_attempt(request)
        ua.mark_dispatched(reservation)
        ua.mark_unresolved(reservation, 'unknown earlier outcome')
        settle = late_settler(reservation, request, usage_from_response, None, None, None)
        if error_reply: settle(error=nt.CopilotRejectedResponse(body, 'fixture'))
        else: settle(result=body)
    with hold(root) as view: row = view.attempt(reservation.attempt_id)
    assert row['state'] == 'settled' and row['settle_reason'] == 'late_receipt'
    assert row['cached_tokens'] == expected and row['cost_usd'] == .25 and row['cost_final']
    assert row['provider_usage'] == evidence(body)
    rows = export_rows(None, [], [(row, 'reserved-time', 'dispatch-time')], {})
    assert all('cached_tokens' not in r for r in rows[:-1])
    assert rows[-1]['cached_tokens'] == expected
    write_journal(restored, rows)
    assert usage_store.migrate_from_journal(restored)['status'] == 'completed'
    from ouroboros.store import close_connections_under
    close_connections_under(restored)
    with hold(restored) as view: imported = view.attempt(reservation.attempt_id)
    assert imported['cached_tokens'] == expected and imported['cost_usd'] == .25
    assert imported['provider_usage'] == evidence(body)


@pytest.mark.parametrize('raw', [{'cached_tokens': 'bad', 'prompt_cache_hit_tokens': 7},
    {'prompt_tokens_details': {'cache_write_tokens': True, 'cache_creation_tokens': 7}}])
def test_zero_usage_body_error_projection_follows_final_free_counters(raw):
    normalized, cost, final = usage_from_response({'error': {'code': 429},
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0, **raw}})
    assert normalized['cached_tokens'] == normalized['cache_write_tokens'] == 0
    assert cost == 0. and final is True
    assert event_token_counts(normalized) == dict.fromkeys(
        ('prompt_tokens', 'completion_tokens', 'cached_tokens', 'cache_write_tokens'), 0)


@pytest.mark.parametrize('details,expected', [({'cache_creation_tokens': 0}, 0),
    ({'cache_write_tokens': True, 'cache_creation_tokens': 7}, None)])
def test_real_responses_search_producer_keeps_normalized_cache_fact(tmp_path, monkeypatch, details, expected):
    from types import SimpleNamespace
    from ouroboros import config, net_transport as nt, usage_accounting as ua
    from ouroboros.tools import search
    calls = []
    body = {'usage': {'input_tokens': 5, 'output_tokens': 1, 'input_tokens_details': details}, 'output': []}
    terminal = SimpleNamespace(model_dump=lambda: copy.deepcopy(body), model='fixture')
    def create(**kw):
        calls.append(kw)
        return iter([SimpleNamespace(type='response.output_text.delta', delta='answer'),
                     SimpleNamespace(type='response.completed', response=terminal)])
    monkeypatch.setattr(nt, 'web_search_openai_client', lambda **kw: SimpleNamespace(
        responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(search, '_resolve_openai_client_settings', lambda: ('fixture', None, 'openai', 'openai'))
    monkeypatch.setattr(search, '_estimate_openai_cost', lambda *args: None)
    monkeypatch.setattr(ua, '_reservation_cost', lambda request: 0.)
    ctx = SimpleNamespace(task_id='search-fixture', task_metadata={}, pending_events=[])
    scope = ua.UsageScope(drive_root=config.DATA_DIR, task_id=ctx.task_id, non_task_operation=True)
    with ua.usage_scope(scope): result = json.loads(search._web_search_openai(ctx, 'fixture query', model='fixture'))
    assert result['answer'] == 'answer' and len(calls) == 1
    event = ctx.pending_events[0]
    assert event['cache_write_tokens'] == expected
    with hold(config.DATA_DIR) as view: row = view.attempt(event['ledger_attempt_ids'][0])
    assert row['state'] == 'settled' and row['cache_write_tokens'] == expected
