from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros import capability_evidence as ce
from ouroboros.copilot_catalog_estimate import CATALOG_ESTIMATE_KEY, catalog_projection
from ouroboros.copilot_model_limits import CATALOG_VERSION, metadata_scope
from ouroboros.llm import LLMClient
from ouroboros.pricing import emit_llm_usage_event


BASE_URL = "https://api.githubcopilot.com"
TARGET = dict(provider="openai-compatible", base_url=BASE_URL, api_key="fixture-account",
              resolved_model="gpt-6.2-sol", catalog_model="openai-compatible::gpt-6.2-sol",
              usage_model="openai-compatible/gpt-6.2-sol")
TARIFF = dict(input_price=2.0, output_price=3.0, cache_read_price=0.25,
              cache_write_price=4.0, cache_write_1h_price=8.0, batch_size=100)
USAGE = dict(prompt_tokens=272, completion_tokens=8, cached_tokens=60, cache_write_tokens=40,
             cache_creation=dict(ephemeral_5m_input_tokens=30, ephemeral_1h_input_tokens=10))


def _evidence(**overrides):
    return SimpleNamespace(**{
        "window_tokens": 1050, "status": ce.STATUS_CONFIRMED, "source": ce.SOURCE_PROVIDER_METADATA,
        "stale": False, "metadata_scope": metadata_scope(BASE_URL, TARGET["api_key"]),
        "pricing_tiers": dict(default=272, long_context=1050),
        "catalog_prices": dict(default=dict(TARIFF), long_context={**TARIFF, "batch_size": 50}),
        "has_long_context_surcharge": True,
        "catalog_details": {"version": CATALOG_VERSION}, "ts": "2026-11-20T14:00:00+00:00",
        **overrides,
    })


@pytest.mark.parametrize("input_total,crossed,tier,amount", [
    (272, False, "default", 5.83),  # (172×2 + 8×3 + 60×.25 + 30×4 + 10×8)/100
    (273, True, "long_context", 11.7),
    (1050, True, "long_context", 42.78),
    (1051, True, None, None),
    (None, None, None, None),
])
def test_advertised_tier_not_actual_billing(input_total, crossed, tier, amount):
    result = catalog_projection(_evidence(), {**USAGE, "prompt_tokens": input_total})
    assert result["crossed_default_threshold"] is crossed
    assert result["catalog_tier"] == tier
    assert result["amount"] == (pytest.approx(amount) if amount is not None else None)


@pytest.mark.parametrize("usage_updates,price_updates,reason", [
    ({"cached_tokens": None}, {}, "unreported_token_counts"),
    ({"completion_tokens": None}, {}, "unreported_token_counts"),
    ({"cache_write_tokens": None}, {}, "unreported_token_counts"),
    ({"cached_tokens": 300}, {}, "inconsistent_input_split"),
    ({"cache_creation": {}, "prompt_cache_ttl": "1h"}, {}, "unreported_cache_write_split"),
    ({"cache_creation": {"ephemeral_5m_input_tokens": 30}}, {}, "unreported_cache_write_split"),
    ({"cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}, {},
     "inconsistent_cache_write_split"),
    ({}, {"batch_size": False}, "unknown_catalog_batch_size"),
    ({}, {"cache_write_1h_price": None}, "unknown_catalog_rate"),
    ({}, {"output_price": float("inf")}, "unknown_catalog_rate"),
    ({}, {"input_price": -1}, "unknown_catalog_rate"),
])
def test_unknown_does_not_invent_zero(usage_updates, price_updates, reason):
    ev = _evidence(catalog_prices={"default": {**TARIFF, **price_updates}})
    result = catalog_projection(ev, {**USAGE, **usage_updates})
    assert result["amount"] is None
    assert result["unknown_reason"] == reason
    assert result["crossed_default_threshold"] is False


def test_explicit_free_and_zero_count_need_no_guess():
    ev = _evidence(catalog_prices={"default": {"input_price": 0, "batch_size": 100}}, has_long_context_surcharge=False)
    facts = catalog_projection(ev, dict(prompt_tokens=1, completion_tokens=0, cached_tokens=0, cache_write_tokens=0))
    assert facts["amount"] == 0
    assert facts["unknown_reason"] is None
    assert facts["has_long_context_surcharge"] is False


def test_unsplit_write_counter_uses_single_published_rate_as_labelled_assumption():
    # Representative Responses shape: a total write count, no 5m/1h breakdown.
    tariff = {"input_price": 200, "output_price": 750, "cache_read_price": 10,
              "cache_write_price": 250, "batch_size": 1_000_000}
    ev = _evidence(pricing_tiers={"default": 272_000, "long_context": 1_000_000},
                   catalog_prices={"long_context": tariff})
    usage = {"prompt_tokens": 520_000, "completion_tokens": 200,
             "cached_tokens": 100_000, "cache_write_tokens": 3_256}
    result = catalog_projection(ev, usage)
    expected = ((520_000 - 100_000 - 3_256) * 200 + 200 * 750
                + 100_000 * 10 + 3_256 * 250) / 1_000_000
    assert result["amount"] == pytest.approx(expected)
    assert result["cache_write_pricing"] == "unsplit_rate_assumed"
    # An unreported counter is still unknown, never silently zero.
    result = catalog_projection(ev, {**usage, "cache_write_tokens": None})
    assert result["amount"] is None and result["unknown_reason"] == "unreported_token_counts"


@pytest.fixture
def cached_catalog(monkeypatch, tmp_path):
    observed = _evidence()
    calls = []

    def cached(drive_root, **route):
        calls.append((drive_root, route))
        return observed

    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(ce, "cached_copilot_evidence", cached, raising=False)
    return observed, calls


def normalized_usage(target=None, *, reported_model=None, prices=True, usage=USAGE):
    response = {
        "model": reported_model,
        "choices": [{"message": {"role": "assistant", "content": "fixture"}}],
        "usage": {**usage, CATALOG_ESTIMATE_KEY: {"amount": 9999}, **({"cost": 0.125} if prices else {})},
    }
    _, usage = LLMClient()._normalize_remote_response(response, TARGET if target is None else target, skip_cost_fetch=True)
    return usage


def test_response_logs_use_the_credential_sent_and_keep_provider_cost_separate(cached_catalog, tmp_path):
    from supervisor.events_budget import _handle_llm_usage

    _, calls = cached_catalog
    usage = normalized_usage()
    assert calls == [(tmp_path, dict(model=TARGET["catalog_model"], base_url=BASE_URL, api_key="fixture-account"))]
    assert usage[CATALOG_ESTIMATE_KEY]["amount"] == pytest.approx(5.83)
    assert usage["cost"] == 0.125 and usage["cost_final"] is True
    events = queue.Queue()
    published = []
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, bridge=SimpleNamespace(push_log=published.append))
    emit_llm_usage_event(events, "fixture", TARGET["usage_model"], usage, usage["cost"])
    _handle_llm_usage(events.get_nowait(), ctx)
    assert published[0][CATALOG_ESTIMATE_KEY] == usage[CATALOG_ESTIMATE_KEY]
    assert published[0]["cost"] == 0.125 and published[0]["cost_final"] is True
    persisted = json.loads((tmp_path / "logs/events.jsonl").read_text())
    assert persisted[CATALOG_ESTIMATE_KEY] == usage[CATALOG_ESTIMATE_KEY]
    assert TARGET["api_key"] not in json.dumps(persisted)
    assert "metadata_scope" not in persisted[CATALOG_ESTIMATE_KEY]


def test_normalized_responses_unsplit_and_chat_unknown_are_distinct(cached_catalog):
    observed, _ = cached_catalog
    observed.pricing_tiers = {"default": 272_000, "long_context": 1_000_000}
    observed.catalog_prices = {"long_context": {"input_price": 200, "output_price": 750,
        "cache_read_price": 10, "cache_write_price": 250, "batch_size": 1_000_000}}
    reported = {"prompt_tokens": 520_000, "completion_tokens": 200,
                "cached_tokens": 100_000, "cache_write_tokens": 3_256}
    gpt = normalized_usage(usage=reported)[CATALOG_ESTIMATE_KEY]
    assert gpt["amount"] == pytest.approx((416_744 * 200 + 200 * 750 + 100_000 * 10 + 3_256 * 250) / 1_000_000)
    assert gpt["cache_write_pricing"] == "unsplit_rate_assumed"
    claude_shape = {**reported, "cache_write_tokens": None}
    unknown = normalized_usage(usage=claude_shape)[CATALOG_ESTIMATE_KEY]
    assert unknown["amount"] is None and unknown["unknown_reason"] == "unreported_token_counts"


def test_ineligible_route_or_model_cannot_use_another_catalog_and_provider_cannot_spoof(cached_catalog):
    _, calls = cached_catalog
    assert CATALOG_ESTIMATE_KEY not in normalized_usage({**TARGET, "base_url": "https://example.test"})
    assert normalized_usage(reported_model="different-model")[CATALOG_ESTIMATE_KEY]["amount"] is None
    assert calls == []
    changed = normalized_usage({**TARGET, "api_key": "another-account"})[CATALOG_ESTIMATE_KEY]
    assert changed["unknown_reason"] == "no_current_catalog"
    assert changed["default_max_prompt_tokens"] is None and changed["amount"] is None


@pytest.mark.parametrize("key,value", [("stale", True), ("source", ce.SOURCE_OWNER_ACK)])
def test_stale_or_owner_asserted_source_never_becomes_tariff(cached_catalog, key, value):
    observed, _ = cached_catalog
    setattr(observed, key, value)
    receipt = normalized_usage(prices=False)[CATALOG_ESTIMATE_KEY]
    assert receipt["amount"] is None and receipt["catalog_tier"] is None


def test_real_main_catalog_cache_feeds_the_resolved_dispatch_target(tmp_path, monkeypatch):
    """Combined-body test: no stubs of either cache API or request target resolution."""
    import httpx

    from ouroboros.gateway.settings import _active_main_route
    from ouroboros.llm_routing import resolve_target

    selected = TARGET["catalog_model"]
    configuration = {"OUROBOROS_MODEL": selected, "OPENAI_COMPATIBLE_BASE_URL": BASE_URL,
                     "OPENAI_COMPATIBLE_API_KEY": TARGET["api_key"]}
    route = _active_main_route(configuration)
    target = resolve_target(selected, configuration)
    assert target["catalog_model"] == route["model"] == selected != target["usage_model"]
    assert target["base_url"] == route["base_url"] == BASE_URL

    calls = []

    def fake_get(url, *, headers, **kwargs):
        calls.append(url)
        assert headers["Authorization"] == f'Bearer {target["api_key"]}'
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": [{
            "id": target["resolved_model"],
            "capabilities": {"limits": {"max_context_window_tokens": 1050, "max_prompt_tokens": 1050,
                                        "max_output_tokens": 64}},
            "billing": {"token_prices": {"default": {**TARIFF, "max_prompt_tokens": 272},
                                         "long_context": {**TARIFF, "max_prompt_tokens": 1050, "batch_size": 50}}},
        }]})

    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(httpx, "get", fake_get)
    evidence = ce.probe(tmp_path, **route, api_key=target["api_key"])
    assert evidence.catalog_prices["default"]["input_price"] == 2.0
    assert normalized_usage(target, reported_model=target["resolved_model"])[CATALOG_ESTIMATE_KEY]["amount"] == pytest.approx(5.83)
    assert normalized_usage({**target, "api_key": "another-account"})[CATALOG_ESTIMATE_KEY]["amount"] is None
    assert calls == [BASE_URL + "/models"]  # Only the probe asked for /models, no telemetry network.
