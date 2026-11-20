"""Copilot catalog facts retain units, account and consumer semantics."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ouroboros import capability_evidence as ce
from ouroboros import copilot_model_limits as cm
from ouroboros import response_limits as rl
from ouroboros.context_view_facts import window_projection
from ouroboros.context_window import resolve_window

BASE = "https://api.business.githubcopilot.com"


def row(model, total, prompt, output, default=None, long=None):
    tiers = {name: {"max_prompt_tokens": value} for name, value in
             (("default", default), ("long_context", long)) if value is not None}
    return {"id": model, "capabilities": {"limits": {"max_context_window_tokens": total,
            "max_prompt_tokens": prompt, "max_output_tokens": output}},
            "billing": {"token_prices": tiers}}


@pytest.mark.parametrize("model,total,prompt,output,default,long", [
    ("gpt-fixture", 1_050_000, 1_050_000, 128_000, 272_000, 1_050_000),
    ("claude-fixture", 1_000_000, 936_000, 128_000, 200_000, 936_000),
    ("gemini-fixture", 1_048_576, 983_040, 65_536, 200_000, 983_040),
    ("single-fixture", 1_048_576, 983_040, 65_536, 983_040, None),
])
def test_catalog_preserves_kinds_and_does_not_make_pricing_threshold_a_limit(model, total, prompt, output, default, long):
    payload = {"data": [row(model, total, prompt, output, default, long)]}
    facts = cm.catalog_limits(payload, "openai-compatible::" + model)
    assert (facts["total"], facts["input"], facts["output"]) == (total, prompt, output)
    assert facts["pricing_tiers"] == {k: v for k, v in (("default", default), ("long_context", long)) if v is not None}
    assert cm.catalog_limits({"data": payload["data"] * 2}, model) is None


def test_invalid_unknown_and_credentials_are_not_invented_or_leaked():
    assert cm.catalog_limits({"data": [row("a", True, "1000", -1)]}, "a") == {
        "total": 0, "input": 0, "output": 0, "non_streaming_output": 0,
        "tokenizer": "", "preferred_tier": "", "pricing_tiers": {}, "version": cm.CATALOG_VERSION}
    assert cm.catalog_limits({"data": []}, "a") is None
    assert cm.metadata_scope(BASE, "") == ""
    assert cm.metadata_scope("https://example.com", "A") == ""
    assert cm.metadata_scope(BASE, "secret-A") != cm.metadata_scope(BASE, "secret-B")
    assert "secret-A" not in cm.metadata_scope(BASE, "secret-A")


def test_real_probe_scope_hot_path_and_consumer_math(tmp_path, monkeypatch):
    import httpx

    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            model = row("gpt-fixture", 1_000_000, 900_000, 70_000, 200_000, 900_000)
            model["capabilities"]["limits"]["max_non_streaming_output_tokens"] = 16_000
            model["capabilities"]["tokenizer"] = "test-tokenizer"
            model["billing"]["token_prices"]["preferred_tier"] = "long_context"
            return {"data": [model]}
    def get(url, *, headers, **kwargs):
        calls.append((url, headers))
        return Response()
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(rl, "_dispatch_route", lambda route: (route["provider"], route["base_url"], route.get("api_key")))
    route = dict(provider="openai-compatible", model="openai-compatible::gpt-fixture", base_url=BASE, api_key="secret-A")
    evidence = ce.probe(tmp_path, **route)
    assert len(calls) == 1 and calls[0][0] == BASE + "/models"
    assert calls[0][1]["X-GitHub-Api-Version"] == cm.CATALOG_VERSION
    assert evidence.response_limit["max_output_tokens"] == 70_000
    assert evidence.response_limit["max_non_streaming_output_tokens"] == 16_000
    assert evidence.catalog_details["preferred_tier"] == "long_context"
    assert evidence.catalog_details["tokenizer"] == "test-tokenizer"
    record = resolve_window(evidence, 65_536, root=tmp_path, observation_fp="unrelated")
    assert (record.input.tokens, record.total.tokens, record.output.tokens, record.usable) == (900_000, 1_000_000, 70_000, 900_000)
    view = window_projection(record)
    assert view["pricing_tiers"] == {"default": 200_000, "long_context": 900_000}
    assert "not hard limits" in view["text"]
    assert "inference uses no catalog version header" in view["text"]
    assert view["catalog_details"]["version"] == cm.CATALOG_VERSION
    output_limit = rl.resolve_response_limit(tmp_path, **{k: route[k] for k in ("provider", "model", "base_url", "api_key")})
    assert output_limit.ceiling(80_000) == 70_000
    assert output_limit.max_non_streaming_output_tokens == 16_000  # advertised, not a new cap
    assert output_limit.ceiling(10_000) == 10_000
    assert len(calls) == 1  # no second fetch for the same account
    other = {**route, "api_key": "secret-B", "allow_fetch": False}
    unknown = ce.probe(tmp_path, **other)
    assert unknown.window_tokens == 0 and not unknown.response_limit["max_output_tokens"]
    assert len(calls) == 1
    refreshed = ce.probe(tmp_path, **{**other, "allow_fetch": True})
    assert refreshed.window_tokens == 1_000_000 and len(calls) == 2
    original_again = ce.probe(tmp_path, **route)
    assert original_again.window_tokens == 1_000_000 and original_again.response_limit["max_output_tokens"] == 70_000
    assert len(calls) == 2  # independent account caches survive switching back
    assert "secret-A" not in json.dumps(ce._load(tmp_path))
    assert "secret-B" not in json.dumps(ce._load(tmp_path))


def test_only_the_disproved_kind_yields_to_accepted_input():
    evidence = ce.CapabilityEvidence(1_000_000, "confirmed", ce.SOURCE_PROVIDER_METADATA, "r",
        declared_limits=[{"kind": "input", "tokens": 400_000}, {"kind": "total", "tokens": 1_000_000}])
    record = resolve_window(evidence, 65_536, bound=False, accepted_input_tokens=427_778)
    assert record.input.tokens == 427_778 and record.input.source == "accepted"
    assert record.total.tokens == 1_000_000 and record.usable == 427_778
    assert {x.tokens for x in record.declared} == {400_000, 1_000_000}


def test_actual_physical_allowance_is_bound_to_sending_account_and_stream_mode(tmp_path, monkeypatch):
    from ouroboros.llm_attempt import bound_reply_allowance

    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    target = {"provider": "openai-compatible", "model": "claude-fixture", "base_url": BASE, "api_key": "account-A"}
    rl.record_metadata_limit(tmp_path, provider=target["provider"], model=target["model"], base_url=BASE,
        maximum=50_000, max_non_streaming_output_tokens=16_000,
        metadata_scope=cm.metadata_scope(BASE, "account-A"))
    assert bound_reply_allowance(target, {"max_tokens": 60_000, "stream": False})["max_tokens"] == 50_000
    assert bound_reply_allowance(target, {"max_tokens": 60_000, "stream": True})["max_tokens"] == 50_000
    assert bound_reply_allowance({**target, "api_key": "account-B"}, {"max_tokens": 60_000})["max_tokens"] == 60_000
    assert bound_reply_allowance(target, {"max_tokens": 8_000})["max_tokens"] == 8_000


def test_main_and_reviewer_use_the_same_account_bound_capacity(tmp_path, monkeypatch):
    from ouroboros import config, context_fit, context_window, model_slots, reviewer_window

    model = "openai-compatible::claude-fixture"
    route = {"provider": "openai-compatible", "model": model, "base_url": BASE,
             "use_local": False, "options": {}, "model_role": "main", "room_primary": None,
             "window_observation_fp": "observed-A"}
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ce, "_route_api_key", lambda _model: "account-A")
    monkeypatch.setattr(rl, "_dispatch_route", lambda route: (route["provider"], route["base_url"], "account-A"))
    monkeypatch.setattr(cm, "fetch_catalog_limits", lambda *a: cm.catalog_limits({"data": [
        row("claude-fixture", 1_000_000, 936_000, 128_000, 200_000, 936_000)]}, model))
    monkeypatch.setattr(context_fit, "_context_route", lambda task: (dict(route), {}))
    monkeypatch.setattr(model_slots, "model_role_option", lambda *a, **k: 0)
    monkeypatch.setattr(reviewer_window, "reviewer_route", lambda *a, **k: ("openai-compatible", BASE))
    monkeypatch.setattr(context_window, "route_observation_scope", lambda *a, **k: "observed-A")
    actual, evidence = context_fit.resolve_context_fit_route({}, allow_fetch=True)
    main = context_window.resolve_window(evidence, 100_000, root=tmp_path,
                                         observation_fp=actual["window_observation_fp"])
    assert (main.input.tokens, main.total.tokens, main.usable) == (936_000, 1_000_000, 900_000)
    reviewer = reviewer_window.reviewer_context_window(model, use_local=False, allow_fetch=False)
    assert reviewer_window.reviewer_input_limit(reviewer, 50_000) == 936_000
    assert reviewer_window.reviewer_input_limit(reviewer, 100_000) == 900_000


def test_catalog_failure_preserves_old_account_evidence_without_crossing_accounts(tmp_path, monkeypatch):
    route = dict(provider="openai-compatible", model="openai-compatible::gpt-fixture", base_url=BASE)
    monkeypatch.setattr(rl, "_dispatch_route", lambda route: (route["provider"], route["base_url"], route.get("api_key")))
    monkeypatch.setattr(cm, "fetch_catalog_limits", lambda *a: cm.catalog_limits({"data": [
        row("gpt-fixture", 1_000_000, 900_000, 70_000, 200_000, 900_000)]}, route["model"]))
    known = ce.probe(tmp_path, **route, api_key="account-A")
    assert known.window_tokens == 1_000_000 and known.response_limit["max_output_tokens"] == 70_000
    def offline(*args):
        raise OSError("offline")
    monkeypatch.setattr(cm, "fetch_catalog_limits", offline)
    stale = ce.probe(tmp_path, **route, api_key="account-A", force=True)
    unknown = ce.probe(tmp_path, **route, api_key="account-B", force=True)
    assert stale.stale and stale.window_tokens == 1_000_000
    assert unknown.status == ce.STATUS_FAILED and unknown.window_tokens == 0
    assert not unknown.response_limit["max_output_tokens"]


def test_catalog_missing_output_does_not_refresh_or_erase_a_prior_output(tmp_path, monkeypatch):
    route = dict(provider="openai-compatible", model="openai-compatible::claude-fixture", base_url=BASE)
    old_stamp = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
    rl.record_metadata_limit(tmp_path, **route, maximum=128_000, observed_at=old_stamp,
        metadata_scope=cm.metadata_scope(BASE, "account-A"))
    partial = cm.catalog_limits({"data": [row("claude-fixture", 1_000_000, 936_000, None)]}, route["model"])
    partial["non_streaming_output"] = 16_000
    monkeypatch.setattr(cm, "fetch_catalog_limits", lambda *a: partial)
    result = rl.resolve_response_limit(tmp_path, **route, api_key="account-A", allow_fetch=True)
    assert result.stale and result.max_output_tokens == 128_000 and result.observed_at == old_stamp
    assert result.ceiling(200_000) == 200_000


def test_response_cache_scopes_account_and_reuses_each_account(tmp_path, monkeypatch):
    route = dict(provider="openai-compatible", model="openai-compatible::claude-fixture", base_url=BASE)
    calls = []
    def fetch(model, base_url, key):
        calls.append(key)
        output = 32_000 if key == "A" else 64_000
        return cm.catalog_limits({"data": [row("claude-fixture", 1_000_000, 936_000, output)]}, model)
    monkeypatch.setattr(cm, "fetch_catalog_limits", fetch)
    a = rl.resolve_response_limit(tmp_path, **route, api_key="A", allow_fetch=True)
    b = rl.resolve_response_limit(tmp_path, **route, api_key="B", allow_fetch=True)
    a_again = rl.resolve_response_limit(tmp_path, **route, api_key="A", allow_fetch=True)
    assert (a.max_output_tokens, b.max_output_tokens, a_again.max_output_tokens) == (32_000, 64_000, 32_000)
    assert calls == ["A", "B"]


def test_catalog_fetch_refreshes_scoped_image_evidence(tmp_path, monkeypatch):
    import httpx
    from ouroboros import vision_routing as vision

    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    class Response:
        def __init__(self, key):
            self.key = key
        def raise_for_status(self):
            pass
        def json(self):
            model = row("fixture", 1_000_000, 900_000, 70_000)
            model["capabilities"]["supports_vision"] = self.key == "A"
            return {"data": [model]}
    monkeypatch.setattr(httpx, "get", lambda url, *, headers, **kwargs:
                        Response(headers["Authorization"].removeprefix("Bearer ")))
    cm.fetch_catalog_limits("fixture", BASE, "A")
    cm.fetch_catalog_limits("fixture", BASE, "B")
    records = ce._read(tmp_path)[vision.EVIDENCE_NAMESPACE]
    def key(account):
        return vision.image_route_scope({"provider": "openai-compatible", "base_url": BASE, "api_key": account})
    assert records[key("A")]["models"]["fixture"] is True
    assert records[key("B")]["models"]["fixture"] is False
    assert key("A") != key("B")


def test_catalog_output_follows_route_options_in_both_metadata_paths(tmp_path, monkeypatch):
    model = "openai-compatible::claude-fixture"
    options = {"context_1m": True}
    route = dict(provider="openai-compatible", model=model, base_url=BASE, options=options, api_key="A")
    facts = cm.catalog_limits({"data": [row("claude-fixture", 1_000_000, 936_000, 70_000)]}, model)
    calls = []
    monkeypatch.setattr(cm, "fetch_catalog_limits", lambda *a: (calls.append(a), facts)[1])
    monkeypatch.setattr(rl, "_dispatch_route", lambda route: (route["provider"], route["base_url"], route.get("api_key")))
    result = rl.resolve_response_limit(tmp_path, **route, allow_fetch=True)
    assert result.max_output_tokens == 70_000 and len(calls) == 1
    ce.probe(tmp_path, **route, allow_fetch=True)
    assert len(calls) == 2  # Context probe stores the same exact output-route identity.
    assert rl.resolve_response_limit(tmp_path, **route, allow_fetch=False).max_output_tokens == 70_000
    unconfigured = {**route, "options": {}}
    assert rl.resolve_response_limit(tmp_path, **unconfigured, allow_fetch=False).max_output_tokens == 0
