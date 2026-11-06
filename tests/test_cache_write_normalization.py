"""Standard cache-write projection agrees with accounting; HTTP is mocked."""
import copy
from types import SimpleNamespace

import pytest

from ouroboros._usage_response import usage_from_response
from ouroboros.event_usage import event_token_counts
from ouroboros.llm import LLMClient
from ouroboros.llm_pricing import add_usage
from ouroboros.usage_store import hold
from tests.test_copilot_native_usage import evidence, native_wire, response  # noqa: F401


CASES = [
    ({}, None),
    ({'cache_write_tokens': 0}, 0),
    ({'cache_write_tokens': 7}, 7),
    ({'cache_creation_input_tokens': 0, 'cache_write_tokens': 7}, 0),
    ({'cache_write_tokens': True}, None),
    ({'cache_write_tokens': 'bad'}, None),
    ({'cache_write_tokens': -2}, 0),
    ({'cache_write_tokens': '3'}, 3),
    ({'cache_write_tokens': 2.5}, 2),  # existing accounting coercion, not a new strict policy
    ({'prompt_tokens_details': {'cache_write_tokens': 0}}, 0),
    ({'prompt_tokens_details': {'cache_creation_tokens': 0}}, 0),
    ({'input_tokens_details': {'cache_creation_input_tokens': 0}}, 0),
    ({'cache_write_tokens': 7, 'prompt_tokens_details': {'cache_write_tokens': 0}}, 0),
    ({'cache_write_tokens': 0, 'prompt_tokens_details': {'cache_write_tokens': 7}}, 7),
    ({'prompt_tokens_details': {'cache_write_tokens': 0, 'cache_creation_tokens': 7}}, 0),
    ({'prompt_tokens_details': {'cache_write_tokens': True, 'cache_creation_tokens': 7}}, None),
    ({'cache_write_tokens': 5, 'prompt_tokens_details': {'cache_write_tokens': True}}, 5),
    ({'cache_write_tokens': 5, 'prompt_tokens_details': {'cache_write_tokens': 'bad'}}, 5),
    ({'prompt_tokens_details': {'cache_write_tokens': None, 'cache_creation_tokens': 7}}, 7),
    ({'cache_write_tokens': 0, 'prompt_tokens_details': 'bad'}, 0),
]


@pytest.mark.parametrize('raw,expected', CASES)
def test_returned_write_projection_uses_existing_accounting_precedence(raw, expected):
    body = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
            'usage': copy.deepcopy(raw)}
    before = copy.deepcopy(body)
    extracted, _, _ = usage_from_response(body)
    assert extracted['cache_write_tokens'] == expected
    _, returned = LLMClient(api_key='fixture')._normalize_remote_response(
        body, {'provider': 'openrouter', 'resolved_model': 'fixture'}, skip_cost_fetch=True)
    assert returned.get('cache_write_tokens') == expected
    assert event_token_counts(returned)['cache_write_tokens'] == expected
    assert body == before


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('case,expected', [
    ('zero', 0), ('unknown', None), ('positive', 7), ('detail_zero', 0), ('alias_zero', 0),
])
def test_public_returned_usage_round_and_total_match_settled_standard_write(
        native_wire, model, asynchronous, stream, case, expected):
    invoke, sent, root, task = native_wire
    body = response(model, None if case == 'unknown' else 7 if case == 'positive' else 0)
    detail = body['usage']['input_tokens_details' if model.startswith('gpt') else 'prompt_tokens_details']
    if case == 'detail_zero': body['usage']['cache_write_tokens'] = 7
    if case == 'alias_zero': detail['cache_creation_tokens'] = 7
    message, usage = invoke(model, body, asynchronous, stream)
    assert message['content'] == 'ok' and len(sent) == 1
    assert usage.get('cache_write_tokens') == expected
    assert usage.get('provider_usage') == evidence(body)
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert row['state'] == 'settled' and row['cache_write_tokens'] == expected
    assert row['cost_usd'] is None and usage['cost'] is None
    assert event_token_counts(usage)['cache_write_tokens'] == expected
    from ouroboros.loop_llm_call import _record_round_cache_facts
    from ouroboros.tools.control_task_results import cache_horizon_note
    accumulated = {}
    _record_round_cache_facts(accumulated, usage, round_idx=1)
    assert accumulated['_last_round_cache_usage']['cache_write_tokens'] == expected
    assert cache_horizon_note(SimpleNamespace(_accumulated_usage=accumulated), 4000) == ''
    total = {}
    add_usage(total, usage)
    assert total['reported_tokens']['cache_write_tokens'] == expected
    assert total['cache_write_tokens'] == (expected or 0)


@pytest.mark.parametrize('model', ['claude-opus-5.5', 'gpt-6.1-sol'])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('stream', [False, True])
def test_returned_and_settled_estimates_use_same_write_count(native_wire, monkeypatch, model, asynchronous, stream):
    from ouroboros import pricing, usage_accounting as ua
    calls = []
    def tariff(model, prompt, completion, *, cache_usage, **kw):
        calls.append(copy.deepcopy(cache_usage))
        return .01 + cache_usage['cache_write_tokens'] * .001  # synthetic tariff, not provider pricing
    monkeypatch.setattr(pricing, 'estimate_cost_optional', tariff)
    monkeypatch.setattr(ua, 'estimate_cost_optional', tariff)
    invoke, sent, root, task = native_wire
    body = response(model, 0)
    body['usage']['cache_write_tokens'] = 7
    _, usage = invoke(model, body, asynchronous, stream)
    with hold(root) as view: row = view.attempt(usage['ledger_attempt_ids'][0])
    assert len(sent) == 1 and len(calls) == 2
    assert [c['cache_write_tokens'] for c in calls] == [0, 0]
    assert usage['cost'] == row['cost_usd'] == .01
    assert not usage['cost_final'] and not row['cost_final']


def test_accounting_output_cost_and_tier_split_remain_exact():
    raw = {'prompt_tokens': 100, 'completion_tokens': 5, 'cost': .25,
           'cache_write_tokens': 7, 'prompt_tokens_details': {'cache_write_tokens': 0},
           'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 4}}
    extracted, cost, final = usage_from_response({'usage': copy.deepcopy(raw)})
    assert extracted == {**raw, 'cached_tokens': None, 'cache_write_tokens': 0,
                         'cache_write_tokens_by_ttl': {'1h': 4}}
    assert cost == .25 and final is True
