"""Copilot model-catalog observations, separate from inference and billing policy.

The versioned catalog advertises route/account-specific limits. A pricing-tier
threshold is not a context limit and an advertisement is not a successful send.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from ouroboros.copilot_responses import copilot_endpoint

CATALOG_VERSION = "2026-08-01"


def metadata_scope(base_url: str, api_key: Any) -> str:
    """Bind cached catalog facts to the key sent, endpoint and requested version; never retain the key."""
    key = str(api_key or "")
    if not key or not copilot_endpoint(base_url):
        return ""
    payload = [str(base_url).rstrip("/"), CATALOG_VERSION, key]
    return sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _positive(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def catalog_limits(payload: Any, model: str) -> dict | None:
    """Extract exact-model advertised values; ambiguous duplicate IDs are not evidence."""
    from ouroboros.provider_models import parse_model_route

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    wanted = {str(model), parse_model_route(model).model}
    matches = [row for row in items if isinstance(row, dict) and str(row.get("id") or "") in wanted]
    if len(matches) != 1:
        return None
    row = matches[0]
    limits = _object(_object(row.get("capabilities")).get("limits"))
    prices = _object(_object(row.get("billing")).get("token_prices"))
    tiers = {}
    for name in ("default", "long_context"):
        tier = _object(prices.get(name))
        threshold = _positive(tier.get("max_prompt_tokens")) or _positive(tier.get("context_max"))
        if threshold:
            tiers[name] = threshold
    return {
        "total": _positive(limits.get("max_context_window_tokens")),
        "input": _positive(limits.get("max_prompt_tokens")),
        "output": _positive(limits.get("max_output_tokens")),
        "non_streaming_output": _positive(limits.get("max_non_streaming_output_tokens")),
        "tokenizer": str(_object(row.get("capabilities")).get("tokenizer") or ""),
        "preferred_tier": str(prices.get("preferred_tier") or ""),
        "pricing_tiers": tiers,
        "version": CATALOG_VERSION,
    }


def fetch_catalog_limits(model: str, base_url: str, api_key: str) -> dict | None:
    """One versioned GET using the actual route credential; never changes inference headers."""
    if not metadata_scope(base_url, api_key):
        return None
    import httpx
    from ouroboros.net_transport import copilot_compatible_headers, verify_kwargs

    headers = {**copilot_compatible_headers(base_url, api_key),
               "Authorization": f"Bearer {api_key}", "X-GitHub-Api-Version": CATALOG_VERSION}
    response = httpx.get(str(base_url).rstrip("/") + "/models", headers=headers,
                         timeout=5.0, **verify_kwargs())
    response.raise_for_status()
    payload = response.json()
    from ouroboros.vision_routing import record_catalog_image_input

    rows = payload.get("data") if isinstance(payload, dict) else payload
    record_catalog_image_input("openai-compatible", base_url, rows,
                               source="the OpenAI-compatible gateway's /models", api_key=api_key)
    return catalog_limits(payload, model)
