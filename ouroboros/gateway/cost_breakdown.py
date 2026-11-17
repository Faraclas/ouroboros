"""Ledger-derived cost projections for the dashboard and root-task detail.

The physical-attempt ledger is the one cost authority; this module projects it
into the compatibility tables the UI reads (by model / api key / model category /
task category), the accounting envelope and the root-task breakdown. The
``gateway.history`` endpoint and ``gateway.tasks`` detail import seams remain.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any, Callable, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

_ACCOUNTING_SUMMARY_FIELDS = (
    "settled_usd",
    "confirmed_usd",
    "estimated_usd",
    "reserved_usd",
    "unresolved_upper_bound_usd",
    "accounted_usd",
    "unknown_unmetered",
    "cost_final",
    # `cost_final`'s DISCLOSED CAUSE travels with the flag it explains — without it
    # the client's "Pending (N open)" text could never render (costs.js reads
    # `accounting.non_final_rows`), so the reason for a non-final cost never
    # reached the owner at all.
    "non_final_rows",
    "attempt_counts",
)

def _compat_cost_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cost": round(float(bucket.get("settled_usd") or 0.0), 6),
        "calls": int(bucket.get("physical_calls") or 0),
        # Keep the compatibility tables honest about rows whose settled dollar
        # amount is zero but whose accounting is still open or undisclosed.
        "unknown_unmetered": int(bucket.get("unknown_unmetered") or 0),
        "non_final_rows": int(bucket.get("non_final_rows") or 0),
        "cost_final": bool(bucket.get("cost_final")),
        "prompt_tokens": int(bucket.get("prompt_tokens") or 0),
        "completion_tokens": int(bucket.get("completion_tokens") or 0),
        "cached_tokens": int(bucket.get("cached_tokens") or 0),
        "cache_write_tokens": int(bucket.get("cache_write_tokens") or 0),
        "prompt_cache_ttls": dict(bucket.get("prompt_cache_ttls") or {}),
    }

def _compat_cost_groups(
    groups: Dict[str, Dict[str, Any]],
    unattributed: Dict[str, Any],
    *,
    group_key: Optional[Callable[[str], str]] = None,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for name, raw_bucket in groups.items():
        if not (
            int(raw_bucket.get("physical_calls") or 0)
            or int(raw_bucket.get("unknown_unmetered") or 0)
            or float(raw_bucket.get("accounted_usd") or 0.0)
        ):
            continue
        key = group_key(str(name)) if group_key else str(name)
        source = _compat_cost_bucket(raw_bucket)
        if key not in result:
            result[key] = source
            continue
        target = result[key]
        for field in (
            "cost", "calls", "unknown_unmetered", "non_final_rows",
            "prompt_tokens", "completion_tokens",
            "cached_tokens", "cache_write_tokens",
        ):
            target[field] += source[field]
        target["cost_final"] = target["cost_final"] and source["cost_final"]
        for ttl, count in source["prompt_cache_ttls"].items():
            target["prompt_cache_ttls"][ttl] = int(target["prompt_cache_ttls"].get(ttl, 0)) + int(count)
    if (
        int(unattributed.get("physical_calls") or 0)
        or int(unattributed.get("unknown_unmetered") or 0)
        or float(unattributed.get("accounted_usd") or 0.0)
    ):
        result["unattributed"] = _compat_cost_bucket(unattributed)
    for bucket in result.values():
        bucket["cost"] = round(float(bucket["cost"]), 6)
    return dict(sorted(result.items(), key=lambda item: item[1]["cost"], reverse=True))

_EXTENSION_FIELDS = ("calls", "unknown_unmetered", "non_final_rows", "prompt_tokens", "completion_tokens")


def _extension_view(groups: list) -> Dict[str, Any]:
    """Recorded extension callbacks, as the ledger's own provenance describes them.

    ``skill`` is the extension, ``surface`` the kind of callback (route / registration / ...), both from the
    row's recorded source (see ``usage_store.extension_callback_key``). The rows carry no price: they are
    *not metered*, which is neither zero nor proof the callback reached no paid service."""
    total = dict.fromkeys(_EXTENSION_FIELDS, 0)
    by_extension: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        skill = str(group.get("skill") or "") or "unknown extension"
        values = {
            "calls": int(group.get("calls") or 0), "unknown_unmetered": int(group.get("unknown") or 0),
            "non_final_rows": int(group.get("non_final") or 0),
            "prompt_tokens": int(group.get("prompt_tokens") or 0),
            "completion_tokens": int(group.get("completion_tokens") or 0),
        }
        entry = by_extension.setdefault(skill, {**dict.fromkeys(_EXTENSION_FIELDS, 0), "surfaces": {}})
        label = str(group.get("surface") or "") or "callback"
        entry["surfaces"][label] = entry["surfaces"].get(label, 0) + values["calls"]
        for field in _EXTENSION_FIELDS:
            entry[field] += values[field]
            total[field] += values[field]
    return {
        **total,
        "by_extension": dict(sorted(by_extension.items(), key=lambda item: item[1]["calls"], reverse=True)),
        "cost": None,
        "cost_note": "Not metered: the recorded callbacks carry no price. That is not zero, and not proof "
                     "no paid service was reached.",
    }


def _move_out_of_ai_tables(tables: Dict[str, Dict[str, Any]], groups: list, degraded: bool, infer_category) -> None:
    """Take each recorded extension callback out of the AI request tables it was folded into.

    The tables are projections; the ledger rows stay as recorded. A bucket left with no calls, no unknown
    rows and no dollars disappears; one left with no open rows is final again unless the ledger is degraded."""
    for group in groups:
        model, provider, category = (str(group.get(name) or "") for name in ("model", "provider", "category"))
        removal = {"calls": int(group.get("calls") or 0), "unknown_unmetered": int(group.get("unknown") or 0),
                   "non_final_rows": int(group.get("non_final") or 0),
                   "prompt_tokens": int(group.get("prompt_tokens") or 0),
                   "completion_tokens": int(group.get("completion_tokens") or 0)}
        keys = {"by_model": model or "unattributed", "by_api_key": provider or "unattributed",
                "by_model_category": infer_category(model) if model else "unattributed",
                "by_task_category": category or "unattributed"}
        for table, key in keys.items():
            bucket = tables[table].get(key)
            if bucket is None:
                continue
            for field, amount in removal.items():
                bucket[field] = max(0, int(bucket.get(field) or 0) - amount)
            if bucket["non_final_rows"] == 0 and not degraded:
                bucket["cost_final"] = True
            if not (bucket["calls"] or bucket["unknown_unmetered"] or bucket.get("cost")):
                del tables[table][key]


def make_cost_breakdown_endpoint(data_dir: pathlib.Path):
    def _cost_breakdown_response() -> JSONResponse:
        try:
            from ouroboros.pricing import infer_model_category
            from ouroboros.usage_accounting import usage_breakdown

            # Display read: the store's summary rows; contention past the short display
            # wait reports accounting unavailable (503), never a zero. Capture extension and total
            # summaries in the SAME snapshot; decode and render only after releasing it.
            breakdown = usage_breakdown(data_dir, allow_stale=True, include_extensions=True)
            extension_groups = breakdown.pop("extension_callback_groups")
            unattributed = dict(breakdown.get("unattributed") or {})
            by_model_raw = dict(breakdown.get("by_model") or {})
            try:
                from supervisor.state import TOTAL_BUDGET_LIMIT

                live_limit = float(TOTAL_BUDGET_LIMIT or 0.0)
            except (ImportError, TypeError, ValueError):
                live_limit = 0.0
            from ouroboros.settings_setup_contract import resolve_total_budget_usd
            resolved_limit = resolve_total_budget_usd()
            limit = live_limit if 0 < live_limit < float("inf") else float(resolved_limit or 0.0)
            degraded = bool(breakdown.get("integrity_degraded"))
            extension = _extension_view(extension_groups)
            tables = {
                "by_model": _compat_cost_groups(by_model_raw, dict(unattributed.get("model") or {})),
                "by_api_key": _compat_cost_groups(
                    dict(breakdown.get("by_provider") or {}),
                    dict(unattributed.get("provider") or {}),
                ),
                "by_model_category": _compat_cost_groups(
                    by_model_raw,
                    dict(unattributed.get("model") or {}),
                    group_key=infer_model_category,
                ),
                "by_task_category": _compat_cost_groups(
                    dict(breakdown.get("by_category") or {}),
                    dict(unattributed.get("category") or {}),
                ),
            }
            _move_out_of_ai_tables(tables, extension_groups, degraded, infer_model_category)
            accounting = {field: breakdown.get(field) for field in _ACCOUNTING_SUMMARY_FIELDS}
            accounting.update({
                # Whole-ledger counts stay whole: extension callbacks are still unmetered rows.
                "extension_unknown_unmetered": extension["unknown_unmetered"],
                "available": True,
                "authority": "physical_attempt_ledger",
                "limit_usd": round(limit, 6),
                # Room above KNOWN (settled) spend, the admission rule's own number.
                "remaining_known_usd": (
                    round(max(0.0, limit - float(breakdown.get("settled_usd") or 0.0)), 6)
                    if limit > 0
                    else None
                ),
            })
            return JSONResponse({
                # Compatibility fields now project the physical-attempt ledger;
                # events.jsonl is import evidence, never a second cost authority.
                "total_cost": round(float(breakdown.get("settled_usd") or 0.0), 6),
                # Requests and unproven dispatches; never relabel an ambiguous row as a known model call.
                "total_calls": max(0, int(breakdown.get("physical_calls") or 0) - extension["calls"]),
                "total_physical_calls": int(breakdown.get("physical_calls") or 0),
                "extension_activity": extension,
                "total_prompt_tokens": max(0, int(breakdown.get("prompt_tokens") or 0) - extension["prompt_tokens"]),
                "total_completion_tokens": max(
                    0, int(breakdown.get("completion_tokens") or 0) - extension["completion_tokens"]),
                "total_cached_tokens": int(breakdown.get("cached_tokens") or 0),
                "total_cache_write_tokens": int(breakdown.get("cache_write_tokens") or 0),
                "prompt_cache_ttls": dict(breakdown.get("prompt_cache_ttls") or {}),
                **tables,
                "accounting": accounting,
                "unattributed": unattributed,
            })
        except Exception:
            log.exception("Physical-attempt accounting unavailable")
            return JSONResponse({
                "error": "Physical-attempt accounting unavailable",
                "accounting": {
                    "available": False,
                    "authority": "physical_attempt_ledger",
                    "cost_final": False,
                    "error_code": "ledger_unavailable",
                },
            }, status_code=503)

    async def api_cost_breakdown(_request: Request) -> JSONResponse:
        """Return ledger-derived cost and physical-attempt breakdowns."""
        # Off the event loop (as ``gateway/state.py`` does): a ledger read, stale
        # path or a cold first replay, must never hold every HTTP client behind it.
        return await asyncio.to_thread(_cost_breakdown_response)

    return api_cost_breakdown


def _task_cost_breakdown_view(drive_root: pathlib.Path, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read-side "where did the money go" projection for a ROOT task's detail.

    Computed from the physical-attempt ledger AT READ TIME and never persisted
    into the task result — the ledger stays the single monetary authority (P7);
    the stored envelope keeps only its existing own/subtree projections.
    ``children_usd`` is subtree − own − unattributed (the subtraction every
    reader had to do by hand); ``delegated`` is a filter over the execution
    axis (subscription sessions), not a third sum. Unavailable accounting
    returns None — the field is simply absent, never a confident $0. That
    covers BOTH an unreadable ledger and a readable one that holds no
    attributable row for this subtree (empty or legacy-only): ``_summary()``
    always returns a float for ``accounted_usd``, so "no accounting happened"
    is decided on the ROW COUNTS, never on the dollar sum being 0.0."""
    task_id = str(result.get("task_id") or "")
    root_id = str(result.get("root_task_id") or "") or task_id
    # Subtree math is ledger-attributable only at the root (child rows carry
    # the ROOT's id, not every ancestor's); non-root details omit the view.
    if not task_id or root_id != task_id:
        return None
    try:
        from ouroboros.cost_projection import honest_accounted_amount
        from ouroboros.usage_accounting import usage_breakdown

        # Read at display time only: the root's summary rows (unavailable under contention).
        breakdown = usage_breakdown(pathlib.Path(result.get("budget_drive_root") or drive_root),
                                    root_task_id=root_id, custody_root=drive_root, allow_stale=True, include_custody=True)
    except Exception:
        log.debug("cost breakdown view unavailable for %s", task_id, exc_info=True)
        return None
    subtree = honest_accounted_amount(breakdown)
    counts = breakdown.get("attempt_counts")
    counts = counts if isinstance(counts, dict) else {}
    # `metadata_only` is a count of AMBIGUOUS legacy calls carrying no money, so
    # it can never make a $0 measured; only priced attempt rows or subscription
    # sessions can. With neither, nothing was accounted for this subtree and the
    # view is ABSENT — the empty/legacy-ledger case that a `0.0 == measured zero`
    # reading would have published as `own 0 / children 0 / cost_final true`.
    priced_rows = sum(int(value or 0) for key, value in counts.items() if key != "metadata_only")
    sessions = int(breakdown.get("subscription_sessions") or 0)
    if subtree is None or (priced_rows <= 0 and sessions <= 0):
        return None
    own_bucket = (breakdown.get("by_task") or {}).get(task_id)
    # No rows attributed to the root itself is a MEASURED zero (all spend was
    # children's), not an unknown — unknowns ride `unknown_unmetered` below.
    own = float(own_bucket.get("accounted_usd") or 0.0) if isinstance(own_bucket, dict) else 0.0
    # Money inside this subtree that no task id claims (legacy/blank-task rows)
    # is DISCLOSED on its own axis instead of being silently folded into the
    # children's share: own + children + unattributed == subtree.
    unattributed_bucket = (breakdown.get("unattributed") or {}).get("task")
    unattributed = (
        float(unattributed_bucket.get("accounted_usd") or 0.0)
        if isinstance(unattributed_bucket, dict) else 0.0
    )
    delegated = breakdown.get("delegated") if isinstance(breakdown.get("delegated"), dict) else {}
    return {
        "own_usd": round(own, 6),
        "children_usd": round(max(0.0, float(subtree) - own - unattributed), 6),
        "unattributed_usd": round(unattributed, 6),
        "delegated_disclosed_usd": round(float(delegated.get("settled_usd") or 0.0), 6),
        # C2: the explicit subtree total under its honest name — an accounted
        # UPPER BOUND (own + children + unattributed), not a settled receipt.
        "accounted_upper_bound_usd": round(float(subtree), 6),
        "subscription_sessions": sessions,
        "unknown_unmetered": breakdown.get("unknown_unmetered"),
        "non_final_rows": breakdown.get("non_final_rows"),
        "cost_final": bool(breakdown.get("cost_final")),
        "cost_custody": breakdown.get("cost_custody"),
        "authority": "physical_attempt_ledger",
    }
