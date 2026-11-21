"""Daily cumulative Viper Copilot spend (America/New_York boundaries, provider-reported nano_aiu only)."""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sqlite3

from ouroboros import usage_accounting as ua
from ouroboros import usage_store
from ouroboros._usage_rows import copilot_daily_delta, copilot_usage_nano_aiu, render_daily_spend
from tests.test_cost_extension_activity import _data_root


def _settle_copilot(root, attempt_id, ts_iso, nano_aiu, *, model="openai-compatible/claude-opus-5.5"):
    with usage_store.hold(root) as txn:
        reserved = txn.write({
            "kind": "attempt",
            "attempt_id": attempt_id,
            "state": "reserved",
            "model": model,
            "provider": "openai-compatible",
            "reservation_upper_bound_usd": None,
            "pricing_known": False,
            "task_id": "t1",
            "root_task_id": "r1",
            "category": "task",
            "source": "test.daily",
            "ts": ts_iso,
        })
        dispatched = txn.write({**reserved, "state": "dispatched", "ts": ts_iso}, reserved)
        settled_row = {
            **dispatched,
            "state": "settled",
            "ts": ts_iso,
            "cost_usd": None,
            "cost_final": False,
            "prompt_tokens": 500,
            "completion_tokens": 100,
        }
        if nano_aiu is not None:
            settled_row["provider_usage"] = {
                "source": "provider_response",
                "copilot_usage": {"total_nano_aiu": nano_aiu},
            }
        txn.write(settled_row, dispatched)


def test_ny_midnight_boundaries_est_and_edt():
    # EST (UTC-5): 2026-11-21T04:59:59Z -> 2026-11-20; 05:00:00Z -> 2026-11-21
    row_before = {
        "state": "settled",
        "ts": "2026-11-21T04:59:59+00:00",
        "provider_usage": {"copilot_usage": {"total_nano_aiu": 1_500_000_000}},
    }
    row_after = {
        "state": "settled",
        "ts": "2026-11-21T05:00:00+00:00",
        "provider_usage": {"copilot_usage": {"total_nano_aiu": 2_500_000_000}},
    }
    assert copilot_daily_delta(row_before) == ("2026-11-20", 1_500_000_000)
    assert copilot_daily_delta(row_after) == ("2026-11-21", 2_500_000_000)

    # EDT (UTC-4): 2026-07-15T03:59:59Z -> 2026-07-14; 04:00:00Z -> 2026-07-15
    edt_before = {**row_before, "ts": "2026-07-15T03:59:59+00:00"}
    edt_after = {**row_after, "ts": "2026-07-15T04:00:00+00:00"}
    assert copilot_daily_delta(edt_before) == ("2026-07-14", 1_500_000_000)
    assert copilot_daily_delta(edt_after) == ("2026-07-15", 2_500_000_000)


def test_strictly_provider_reported_nano_aiu_only_no_token_estimation(tmp_path, monkeypatch):
    root = _data_root(tmp_path, monkeypatch)
    # Streaming GPT attempt with tokens only (no copilot_usage.total_nano_aiu) must NOT be estimated.
    _settle_copilot(root, "gpt-stream-no-credits", "2026-11-05T15:00:00+00:00", None, model="openai-compatible/gpt-6.2-sol")
    # Prior month attempt must be omitted from November.
    _settle_copilot(root, "oct-call", "2026-10-31T20:00:00+00:00", 5_000_000_000)
    # First November provider-reported credit call: Nov 6 (10.5 credits = $0.105).
    _settle_copilot(root, "nov6-a", "2026-11-06T21:00:00+00:00", 10_500_000_000)
    # Nov 7 has no reported calls (gap day -> carries forward cumulative with 0 daily).
    # Nov 8 has two calls straddling UTC midnight but both in Nov 8 EST (23:30 EST = Nov 9 04:30Z).
    _settle_copilot(root, "nov8-a", "2026-11-08T16:00:00+00:00", 2_000_000_000)
    _settle_copilot(root, "nov8-b", "2026-11-09T04:30:00+00:00", 3_000_000_000)
    # Future day relative to `now` (Nov 10) must be omitted.
    _settle_copilot(root, "nov10-future", "2026-11-10T15:00:00+00:00", 9_000_000_000)

    now = _dt.datetime(2026, 11, 9, 18, 0, 0, tzinfo=_dt.timezone.utc)  # 2026-11-09 13:00 EST
    breakdown = ua.usage_breakdown(root, include_daily_spend=True, daily_spend_now=now)
    ds = breakdown["daily_spend"]

    assert ds["available"] is True
    assert ds["timezone"] == "America/New_York"
    assert ds["month"] == "2026-11"
    assert ds["today"] == "2026-11-09"
    assert ds["credit_usd"] == 0.01
    assert ds["first_day"] == "2026-11-06"
    # Nov 1-5 omitted (no provider-reported credits); Nov 10 omitted (future).
    assert [d["date"] for d in ds["days"]] == ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"]
    assert ds["days"][0] == {
        "date": "2026-11-06",
        "viper_daily_credits": 10.5,
        "viper_cumulative_credits": 10.5,
        "viper_daily_usd": 0.105,
        "viper_cumulative_usd": 0.105,
        "viper_calls": 1,
    }
    assert ds["days"][1] == {
        "date": "2026-11-07",
        "viper_daily_credits": 0.0,
        "viper_cumulative_credits": 10.5,
        "viper_daily_usd": 0.0,
        "viper_cumulative_usd": 0.105,
        "viper_calls": 0,
    }
    assert ds["days"][2] == {
        "date": "2026-11-08",
        "viper_daily_credits": 5.0,
        "viper_cumulative_credits": 15.5,
        "viper_daily_usd": 0.05,
        "viper_cumulative_usd": 0.155,
        "viper_calls": 2,
    }
    assert ds["days"][3]["viper_cumulative_credits"] == 15.5
    assert ds["days"][3]["viper_daily_credits"] == 0.0
    assert ds["total_viper_credits"] == 15.5
    assert ds["total_viper_usd"] == 0.155
    assert ds["total_viper_calls"] == 3


def test_invalid_or_released_rows_contribute_no_copilot_credits():
    for bad in (
        {"state": "released", "provider_usage": {"copilot_usage": {"total_nano_aiu": 1_000_000_000}}},
        {"state": "settled", "provider_usage": {"copilot_usage": {"total_nano_aiu": True}}},
        {"state": "settled", "provider_usage": {"copilot_usage": {"total_nano_aiu": -1}}},
        {"state": "settled", "provider_usage": {"copilot_usage": {"total_nano_aiu": float("nan")}}},
        {"state": "settled", "provider_usage": {}},
    ):
        assert copilot_usage_nano_aiu(bad) is None


def test_boot_fold_for_older_usage_store_and_endpoint_contract(tmp_path, monkeypatch):
    from ouroboros.gateway.cost_breakdown import make_cost_breakdown_endpoint

    root = _data_root(tmp_path, monkeypatch)
    now_utc = _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0)
    ts_iso = now_utc.isoformat()
    _settle_copilot(root, "today-a", ts_iso, 4_200_000_000)

    # Simulate an older store created before daily_copilot_credits existed.
    with sqlite3.connect(root / usage_store.STORE_REL) as conn:
        conn.execute("DROP TABLE daily_copilot_credits")
        conn.execute("DELETE FROM meta WHERE key='daily_copilot_credits'")

    # Endpoint still returns 200 with existing accounting intact and honest daily_spend.available=False.
    pending_resp = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    assert pending_resp.status_code == 200
    pending = json.loads(pending_resp.body)
    assert pending["accounting"]["available"] is True
    assert pending["daily_spend"]["available"] is False
    assert pending["daily_spend"]["reason"] == "fold_pending"
    assert pending["daily_spend"]["total_viper_credits"] is None

    # Lifespan boot folds once, idempotently.
    first = usage_store.migrate_from_journal(root, initialize_journal=True)
    second = usage_store.migrate_from_journal(root, initialize_journal=True)
    assert first["daily_copilot_credits"] == "folded"
    assert second["daily_copilot_credits"] == "present"

    ready_resp = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    assert ready_resp.status_code == 200
    ready = json.loads(ready_resp.body)
    assert ready["daily_spend"]["available"] is True
    assert ready["daily_spend"]["total_viper_credits"] == 4.2
    assert ready["daily_spend"]["total_viper_usd"] == 0.042
    assert ready["daily_spend"]["total_viper_calls"] == 1


def test_render_daily_spend_empty_and_unavailable():
    now = _dt.datetime(2026, 11, 21, 15, 0, 0, tzinfo=_dt.timezone.utc)
    unavail = render_daily_spend(None, now=now)
    assert unavail["available"] is False and unavail["total_viper_credits"] is None
    empty = render_daily_spend([], now=now)
    assert empty["available"] is True and empty["first_day"] is None and empty["days"] == []
    assert empty["total_viper_credits"] == 0.0 and empty["total_viper_usd"] == 0.0
