"""One Copilot response's advertised tariff projection, never a billing fact."""
from __future__ import annotations

import math

from ouroboros.event_usage import event_token_counts, token_count


CATALOG_ESTIMATE_KEY = "copilot_catalog_estimate"


def _positive_int(value):
    return value if type(value) is int and value > 0 else None


def _estimate(tariff: dict, counts: dict, usage: dict) -> tuple[float | None, str | None]:
    prompt, output, read, write = (counts[k] for k in (
        "prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"))
    if None in (prompt, output, read, write):
        return None, "unreported_token_counts"
    regular = prompt - read - write
    if regular < 0:
        return None, "inconsistent_input_split"
    quantities = {"input_price": regular, "output_price": output, "cache_read_price": read}
    if write:
        if "cache_write_1h_price" not in tariff:
            # A single advertised write rate supplies a useful provisional
            # estimate; the catalog does not prove which retention was billed.
            quantities["cache_write_price"] = write
        else:
            # A requested retention or an aggregate write count does not prove a TTL.
            # Only the response's explicit 5m/1h split can discriminate these rates.
            split = usage.get("cache_creation")
            if not isinstance(split, dict):
                return None, "unreported_cache_write_split"
            short = token_count(split.get("ephemeral_5m_input_tokens"))
            long = token_count(split.get("ephemeral_1h_input_tokens"))
            if None in (short, long):
                return None, "unreported_cache_write_split"
            if short + long != write:
                return None, "inconsistent_cache_write_split"
            quantities.update(cache_write_price=short, cache_write_1h_price=long)

    batch = _positive_int(tariff.get("batch_size"))
    if batch is None:
        return None, "unknown_catalog_batch_size"
    terms = []
    try:
        for field, count in quantities.items():
            if not count:
                continue  # An explicit zero needs no rate; an absent count never gets here.
            rate = tariff.get(field)
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                return None, "unknown_catalog_rate"
            terms.append((count / batch) * rate)
        amount = math.fsum(terms)
    except OverflowError:
        return None, "out_of_range_estimate"
    return (amount, None) if math.isfinite(amount) else (None, "out_of_range_estimate")


def catalog_projection(evidence, usage: dict) -> dict:
    """Use a whole-tier linear token estimate, not marginal/block-rounded charges.

    Equality stays in the default tier; a higher prompt can cross that threshold
    even if no long-context rate is published. An out-of-range or unknown prompt
    has no inferable catalog tier. This describes the catalog, not the actual tier.
    """
    counts = event_token_counts(usage)
    prices = getattr(evidence, "catalog_prices", None) or {}
    thresholds = getattr(evidence, "pricing_tiers", None) or {}
    standard = _positive_int(thresholds.get("default"))
    extended = _positive_int(thresholds.get("long_context"))
    prompt = counts["prompt_tokens"]
    crossed = prompt > standard if prompt is not None and standard is not None else None
    tier = None
    if crossed is False:
        tier = "default"
    elif crossed and extended is not None and prompt <= extended:
        tier = "long_context"
    tariff = prices.get(tier)
    if evidence is None:
        amount, reason = None, "no_current_catalog"
    elif tier is None:
        amount, reason = None, "unknown_catalog_tier"
    elif not isinstance(tariff, dict):
        amount, reason = None, "unknown_catalog_rate"
    else:
        amount, reason = _estimate(tariff, counts, usage)
    surcharge = getattr(evidence, "has_long_context_surcharge", None)
    return {
        "catalog_tier": tier,
        "crossed_default_threshold": crossed,
        "has_long_context_surcharge": surcharge if isinstance(surcharge, bool) else None,
        "default_max_prompt_tokens": standard,
        "long_context_max_prompt_tokens": extended,
        "prompt_tokens": prompt,
        "amount": amount,
        "cache_write_pricing": ("unsplit_rate_assumed" if amount is not None and counts["cache_write_tokens"]
                                and "cache_write_1h_price" not in tariff else None),
        "unknown_reason": reason,
        "catalog_version": (getattr(evidence, "catalog_details", None) or {}).get("version"),
        "catalog_observed_at": getattr(evidence, "ts", None),
    }


def response_catalog_estimate(target: dict, response: dict, usage: dict) -> dict | None:
    """Reuse only cached metadata bound to this send's endpoint, model, key and source."""
    from ouroboros.copilot_model_limits import metadata_scope
    from ouroboros.copilot_responses import copilot_endpoint

    endpoint = str(target.get("base_url") or "")
    if target.get("provider") != "openai-compatible" or not copilot_endpoint(endpoint):
        return None
    resolved = target.get("resolved_model")
    model = target.get("catalog_model")
    reported_model = response.get("model")
    if not resolved or not model or (reported_model and reported_model != resolved):
        # Provider aliases are not a witness of a different exact catalog row.
        return {**catalog_projection(None, usage), "unknown_reason": "unbound_response_model"}
    key = target.get("api_key")
    scope = metadata_scope(endpoint, key)
    evidence = None
    if scope:
        from ouroboros.capability_evidence import (
            SOURCE_PROVIDER_METADATA, cached_copilot_evidence, canonical_evidence_root, is_known,
        )

        observed = cached_copilot_evidence(canonical_evidence_root(), model=model, base_url=endpoint, api_key=key)
        if (is_known(observed, require_fresh=True) and observed.source == SOURCE_PROVIDER_METADATA
                and observed.metadata_scope == scope):
            evidence = observed
    return catalog_projection(evidence, usage)
