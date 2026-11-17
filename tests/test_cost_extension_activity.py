"""Extension-host callbacks are shown apart from AI model requests; the ledger itself is untouched."""
from __future__ import annotations

import asyncio
import json

from ouroboros import usage_accounting as ua
from ouroboros import usage_store


def _data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "state" / "state.json").write_text(json.dumps({"spent_usd": 0.0, "spent_calls": 0}), encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "7.5")
    usage_store.migrate_from_journal(root)
    return root


def _extension_callback(root, dispatch_id, surface, skill="copilot-credits", name="usage"):
    ua.record_unmetered_external_dispatch(
        dispatch_id, drive_root=root, provider="external-extension", task_id=f"extension:{skill}",
        root_task_id=f"extension:{skill}", category="external_skill",
        source=f"extension_{surface}:{skill}:{name}", non_task_operation=True)


def _breakdown(root):
    from ouroboros.gateway.cost_breakdown import make_cost_breakdown_endpoint

    response = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    assert response.status_code == 200
    return json.loads(response.body)


def _real_attempt(root):
    reservation = ua.reserve_attempt(ua.AttemptRequest(
        model="openai/gpt-5.2", provider="openai", reservation_usd=0.5, global_limit_usd=7.5, drive_root=root,
        task_id="real", root_task_id="root-1", category="task", source="test.gateway"))
    ua.mark_dispatched(reservation)
    ua.settle_attempt(reservation, {"prompt_tokens": 10, "completion_tokens": 2}, cost_usd=0.25, cost_final=True)


def test_extension_callbacks_leave_the_ai_tables_but_not_the_ledger(tmp_path, monkeypatch):
    root = _data_root(tmp_path, monkeypatch)
    _real_attempt(root)
    for index in range(2):
        _extension_callback(root, f"route-{index}", "route")
    _extension_callback(root, "registration", "registration", name="register")
    # Unproven provenance: an external row that is NOT an extension callback stays where it was recorded.
    ua.record_unmetered_external_dispatch("skill-call", drive_root=root, provider="external-skill",
                                          task_id="skill", root_task_id="root-1", category="skill", source="other")

    payload = _breakdown(root)

    assert payload["total_physical_calls"] == 5
    assert payload["total_calls"] == 2  # the real attempt and the unproven external row
    extension = payload["extension_activity"]
    assert extension["calls"] == 3 and extension["unknown_unmetered"] == 3 and extension["cost"] is None
    assert extension["by_extension"] == {"copilot-credits": {
        "calls": 3, "unknown_unmetered": 3, "non_final_rows": 3, "prompt_tokens": 0, "completion_tokens": 0,
        "surfaces": {"route": 2, "registration": 1}}}
    assert "external-extension" not in payload["by_api_key"]
    assert payload["by_api_key"]["external-skill"]["calls"] == 1
    assert payload["by_model"]["unattributed"]["calls"] == 1
    assert "external_skill" not in payload["by_task_category"]
    assert payload["by_model"]["openai/gpt-5.2"]["calls"] == 1
    # The whole-ledger count keeps every unmetered row; the extension share is disclosed beside it.
    assert payload["accounting"]["unknown_unmetered"] == 4
    assert payload["accounting"]["extension_unknown_unmetered"] == 3
    with usage_store.read(root) as txn:
        assert txn.bucket("kind", "external_unmetered").physical == 4  # recorded rows are unchanged


def test_ledger_with_only_extension_callbacks_has_no_phantom_ai_rows(tmp_path, monkeypatch):
    root = _data_root(tmp_path, monkeypatch)
    _extension_callback(root, "only", "route")

    payload = _breakdown(root)

    assert payload["total_calls"] == 0 and payload["total_physical_calls"] == 1
    assert payload["by_model"] == {} and payload["by_api_key"] == {}
    assert payload["by_task_category"] == {} and payload["by_model_category"] == {}
    assert payload["extension_activity"]["calls"] == 1
    assert payload["accounting"]["cost_final"] is False  # an unmetered row is still disclosed as open


def test_store_an_older_body_recorded_is_folded_once_at_boot(tmp_path, monkeypatch):
    import sqlite3

    root = _data_root(tmp_path, monkeypatch)
    _extension_callback(root, "old-1", "route")
    _extension_callback(root, "old-2", "registration", name="register")
    # An older body kept no extension scope: drop it and the marker, leaving only the recorded rows.
    with sqlite3.connect(root / usage_store.STORE_REL) as conn:
        conn.execute("DELETE FROM summaries WHERE scope='extension'")
        conn.execute("DELETE FROM meta WHERE key='extension_summaries'")
    from ouroboros.gateway.cost_breakdown import make_cost_breakdown_endpoint
    assert asyncio.run(make_cost_breakdown_endpoint(root)(None)).status_code == 503

    first = usage_store.migrate_from_journal(root, initialize_journal=True)
    second = usage_store.migrate_from_journal(root, initialize_journal=True)

    assert first["extension_summaries"] == "folded" and second["extension_summaries"] == "present"
    extension = _breakdown(root)["extension_activity"]
    assert extension["calls"] == 2 and extension["by_extension"]["copilot-credits"]["surfaces"] == {"route": 1, "registration": 1}
    with usage_store.read(root) as txn:
        assert txn.bucket("kind", "external_unmetered").physical == 2  # the fold only reads recorded rows


def test_empty_ledger_reports_no_extension_activity(tmp_path, monkeypatch):
    root = _data_root(tmp_path, monkeypatch)

    payload = _breakdown(root)

    assert payload["extension_activity"]["calls"] == 0 and payload["extension_activity"]["by_extension"] == {}


def test_cost_split_uses_one_money_snapshot(tmp_path, monkeypatch):
    import contextlib

    root = _data_root(tmp_path, monkeypatch)
    _real_attempt(root)
    _extension_callback(root, 'route', 'route')
    original = usage_store.read
    reads = []

    @contextlib.contextmanager
    def counted(*args, **kwargs):
        reads.append(1)
        with original(*args, **kwargs) as txn:
            yield txn

    monkeypatch.setattr(usage_store, 'read', counted)
    payload = _breakdown(root)
    assert payload['total_calls'] == 1 and payload['extension_activity']['calls'] == 1
    assert len(reads) == 1


def test_incomplete_boot_fold_is_unavailable_not_a_partial_split(tmp_path, monkeypatch):
    import sqlite3
    from ouroboros.gateway.cost_breakdown import make_cost_breakdown_endpoint

    root = _data_root(tmp_path, monkeypatch)
    _extension_callback(root, 'old', 'route')
    with sqlite3.connect(root / usage_store.STORE_REL) as conn:
        conn.execute("DELETE FROM summaries WHERE scope='extension'")
        conn.execute("DELETE FROM meta WHERE key='extension_summaries'")
    _extension_callback(root, 'new', 'registration')
    response = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    assert response.status_code == 503
    assert json.loads(response.body)['accounting']['available'] is False


def test_extension_key_preserves_axis_literals_and_rejects_incomplete_sources():
    base = {'kind': 'external_unmetered', 'provider': 'external-extension',
            'source': 'extension_route:other-addon:/usage', 'category': 'a|b'}
    key = usage_store.extension_callback_key(base)
    assert json.loads(key) == ['other-addon', 'route', '', 'a|b']
    assert key != usage_store.extension_callback_key({**base, 'category': 'a/b'})
    for source in ('extension_', 'extension_route', 'extension_route:',
                   'extension_route::/usage', 'extension_route:addon:', 'extension_route:addon'):
        assert usage_store.extension_callback_key({**base, 'source': source}) is None
    assert usage_store.extension_callback_key({**base, 'model': 'genuine-model'}) is None


def test_non_copilot_extension_and_unproven_model_are_kept_distinct(tmp_path, monkeypatch):
    root = _data_root(tmp_path, monkeypatch)
    _extension_callback(root, 'other', 'registration', skill='other-addon')
    ua.record_unmetered_external_dispatch('opaque', drive_root=root, provider='external-skill',
                                         category='external_skill', source='skill_exec:addon:run')
    payload = _breakdown(root)
    assert payload['extension_activity']['by_extension']['other-addon']['calls'] == 1
    assert payload['total_calls'] == 1 and payload['by_model']['unattributed']['calls'] == 1
    assert payload['accounting']['unknown_unmetered'] == 2
