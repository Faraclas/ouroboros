"""Ordinary Costs SPA over real disposable ledger/API results, without provider traffic."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest

from tests.test_cost_extension_activity import _data_root, _extension_callback, _real_attempt
from tests.test_subscription_setup_browser import subscription_ui as subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_costs_callback_separation_and_unavailable(subscription_ui, tmp_path, monkeypatch, theme, width):
    from ouroboros.gateway.cost_breakdown import make_cost_breakdown_endpoint
    from ouroboros import usage_accounting as ua

    ui = subscription_ui
    root = _data_root(tmp_path, monkeypatch)
    _real_attempt(root)
    _extension_callback(root, 'route', 'route')
    _extension_callback(root, 'registration', 'registration')
    _extension_callback(root, 'other', 'route', skill='another-addon')
    ua.record_unmetered_external_dispatch('unproven', drive_root=root, provider='external-skill',
                                         category='external_skill', source='skill_exec:addon:run')
    endpoint = make_cost_breakdown_endpoint(root)
    page = ui['new_page'](viewport={'width': width, 'height': 1000}, color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
    responses = []
    unavailable = False

    def costs(route):
        if unavailable:
            route.fulfill(status=503, content_type='application/json', body=json.dumps({'accounting': {'available': False}}))
        else:
            # Playwright's sync bridge already owns an event loop on this thread.
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(lambda: asyncio.run(endpoint(None))).result(timeout=10)
            responses.append(json.loads(response.body))
            route.fulfill(status=response.status_code, content_type='application/json', body=response.body.decode())

    page.route('**/api/cost-breakdown', costs)
    page.goto(ui['url'] + '/')
    page.wait_for_selector('#page-chat')
    if width < 720:
        page.locator('.page.active [data-mobile-nav-toggle]:visible').click()
    page.locator('[data-nav-page="dashboard"]').click()
    page.locator('[data-dashboard-tab="costs"]').click()
    page.wait_for_function("document.getElementById('cost-extension-calls')?.textContent === '3'")
    assert page.locator('#cost-calls').inner_text() == '2'
    assert 'copilot-credits' not in page.locator('#cost-by-model').inner_text()
    assert 'unattributed' in page.locator('#cost-by-model').inner_text()
    assert 'copilot-credits' in page.locator('#cost-by-extension').inner_text()
    assert 'another-addon' in page.locator('#cost-by-extension').inner_text()
    assert 'Not metered' in page.locator('#cost-extension-activity').inner_text()
    assert responses[-1]['total_physical_calls'] == 5
    assert responses[-1]['accounting']['unknown_unmetered'] == 4
    box = page.locator('#page-costs > .costs-scroll')
    assert box.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    shots = os.environ.get('OUROBOROS_UI_EVIDENCE_DIR')
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.locator('#cost-extension-activity').scroll_into_view_if_needed()
        page.screenshot(path=str(Path(shots) / f'costs-{theme}-{width}.png'))
    unavailable = True
    page.locator('.copilot-app-budget summary').click()
    page.locator('#btn-refresh-costs').click()
    page.wait_for_function("document.getElementById('cost-final')?.textContent === 'Unavailable'")
    assert page.locator('#cost-by-extension tbody').inner_text() == 'Unavailable'
    assert page.locator('#cost-extension-calls').inner_text() == '—'
    assert not ui['posts'], 'the view must not save settings or send messages'


@pytest.mark.parametrize('theme,width,height', [
    ('dark', 1360, 1000),
    ('light', 1360, 1000),
    ('dark', 1024, 1000),
    ('dark', 2711, 1623),
])
def test_costs_model_names_and_amounts_unclipped_on_desktop(subscription_ui, theme, width, height):
    ui = subscription_ui
    models = {
        'openai-compatible/claude-opus-5.5': {'calls': 10291, 'cost': 0.0, 'unknown_unmetered': 10292, 'cost_final': False},
        'openai-compatible/claude-sonnet-5.5': {'calls': 4593, 'cost': 0.0, 'unknown_unmetered': 4593, 'cost_final': False},
        'openai-compatible/gemini-4-hydrogen': {'calls': 427, 'cost': 0.0, 'unknown_unmetered': 427, 'cost_final': False},
        'openai-compatible/gpt-6.2-sol': {'calls': 43812, 'cost': 0.0, 'unknown_unmetered': 43812, 'cost_final': False},
        'openai/gpt-5.6-terra': {'calls': 4, 'cost': 0.0, 'unknown_unmetered': 4, 'cost_final': False},
    }
    payload = {
        'accounting': {
            'available': True,
            'settled_usd': 0.0,
            'confirmed_usd': 0.0,
            'estimated_usd': 0.0,
            'reserved_usd': 0.0,
            'unresolved_upper_bound_usd': 0.0,
            'unknown_unmetered': 67713,
            'limit_usd': 20000.0,
            'cost_final': False,
            'non_final_rows': 0,
        },
        'total_calls': 67713,
        'total_cost': 0.0,
        'by_model': models,
        'by_api_key': {
            'openai': {'calls': 8, 'cost': 0.0, 'unknown_unmetered': 8, 'cost_final': False},
            'openai-compatible': {'calls': 67705, 'cost': 0.0, 'unknown_unmetered': 67705, 'cost_final': False},
        },
        'by_model_category': {'flagship': {'calls': 14884, 'cost': 0.0, 'unknown_unmetered': 14884, 'cost_final': False}},
        'by_task_category': {'task': {'calls': 67713, 'cost': 0.0, 'unknown_unmetered': 67713, 'cost_final': False}},
        'extension_activity': {'calls': 0, 'by_extension': {}, 'cost_note': ''},
    }
    page = ui['new_page'](viewport={'width': width, 'height': height}, color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
    page.route('**/api/cost-breakdown', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(payload),
    ))
    page.goto(ui['url'] + '/')
    page.wait_for_selector('#page-chat')
    page.locator('[data-nav-page="dashboard"]').click()
    page.locator('[data-dashboard-tab="costs"]').click()
    page.wait_for_function("document.querySelectorAll('#cost-by-model tbody tr').length === 5")

    name_cells = page.locator('#cost-by-model tbody td.cost-cell-name')
    assert name_cells.count() == len(models)
    for idx, expected_name in enumerate(models):
        cell = name_cells.nth(idx)
        assert cell.inner_text() == expected_name
        metrics = cell.evaluate('(el) => ({ sw: el.scrollWidth, cw: el.clientWidth, to: getComputedStyle(el).textOverflow })')
        assert metrics['to'] != 'ellipsis', metrics
        assert metrics['sw'] <= metrics['cw'] + 1, (expected_name, metrics)

    for wrapper in page.locator('.costs-tables-grid > div').all():
        box = wrapper.evaluate('(el) => ({ sw: el.scrollWidth, cw: el.clientWidth })')
        assert box['sw'] <= box['cw'] + 1, (width, box)

    shots = os.environ.get('OUROBOROS_UI_EVIDENCE_DIR')
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.locator('#cost-by-model').scroll_into_view_if_needed()
        page.screenshot(path=str(Path(shots) / f'costs-models-{theme}-{width}.png'))
    assert not ui['posts'], 'the view must not save settings or send messages'


@pytest.mark.parametrize('theme,width,height', [
    ('dark', 1360, 1000),
    ('light', 1360, 1000),
    ('dark', 390, 900),
    ('light', 390, 900),
])
def test_costs_monthly_spend_chart_themes_toggle_and_accessibility(subscription_ui, theme, width, height):
    ui = subscription_ui
    viper_days = [
        ('2026-11-06', 240.0, 240.0, 18),
        ('2026-11-07', 195.5, 435.5, 14),
        ('2026-11-08', 310.0, 745.5, 22),
        ('2026-11-09', 0.0, 745.5, 0),
        ('2026-11-10', 280.0, 1025.5, 20),
        ('2026-11-11', 215.0, 1240.5, 16),
        ('2026-11-12', 190.0, 1430.5, 15),
        ('2026-11-13', 260.0, 1690.5, 19),
        ('2026-11-14', 175.0, 1865.5, 12),
        ('2026-11-15', 220.0, 2085.5, 17),
        ('2026-11-16', 185.0, 2270.5, 14),
        ('2026-11-17', 210.0, 2480.5, 16),
        ('2026-11-18', 245.0, 2725.5, 19),
        ('2026-11-19', 165.0, 2890.5, 11),
        ('2026-11-20', 195.0, 3085.5, 15),
        ('2026-11-21', 125.0, 3210.5, 9),
    ]
    cost_payload = {
        'accounting': {
            'available': True, 'settled_usd': 0.0, 'confirmed_usd': 0.0, 'estimated_usd': 0.0,
            'reserved_usd': 0.0, 'unresolved_upper_bound_usd': 0.0, 'unknown_unmetered': 237,
            'limit_usd': 20000.0, 'cost_final': False, 'non_final_rows': 0,
        },
        'total_calls': 237, 'total_cost': 0.0,
        'by_model': {'openai-compatible/claude-opus-5.5': {'calls': 237, 'cost': 0.0, 'unknown_unmetered': 237, 'cost_final': False}},
        'by_api_key': {'openai-compatible': {'calls': 237, 'cost': 0.0, 'unknown_unmetered': 237, 'cost_final': False}},
        'by_model_category': {'flagship': {'calls': 237, 'cost': 0.0, 'unknown_unmetered': 237, 'cost_final': False}},
        'by_task_category': {'task': {'calls': 237, 'cost': 0.0, 'unknown_unmetered': 237, 'cost_final': False}},
        'extension_activity': {'calls': 0, 'by_extension': {}, 'cost_note': ''},
        'daily_spend': {
            'available': True, 'timezone': 'America/New_York', 'month': '2026-11', 'today': '2026-11-21',
            'credit_usd': 0.01, 'nano_aiu_per_credit': 1_000_000_000, 'first_day': '2026-11-06',
            'days': [
                {
                    'date': d, 'viper_daily_credits': daily, 'viper_cumulative_credits': cum,
                    'viper_daily_usd': round(daily * 0.01, 6), 'viper_cumulative_usd': round(cum * 0.01, 6),
                    'viper_calls': calls,
                }
                for d, daily, cum, calls in viper_days
            ],
            'total_viper_credits': 3210.5, 'total_viper_usd': 32.105, 'total_viper_calls': 237,
            'basis': 'Provider-reported copilot_usage.total_nano_aiu only (no token estimation); 1 credit = $0.01 consumption value, not an invoice.',
        },
    }
    history_payload = {
        'schema_version': 1, 'status': 'ok', 'source': 'github.copilot_internal.user',
        'timezone': 'America/New_York', 'month': '2026-11', 'today': '2026-11-21',
        'credit_usd': 0.01, 'basis': '1 credit = $0.01 consumption value; not an invoice',
        'days': [
            {'date': '2026-11-18', 'credits_used': 10200.0, 'entitlement': 12600, 'observed_at': '2026-11-18T22:00:00Z', 'login': 'Faraclas'},
            {'date': '2026-11-19', 'credits_used': 10820.0, 'entitlement': 12600, 'observed_at': '2026-11-19T22:00:00Z', 'login': 'Faraclas'},
            {'date': '2026-11-20', 'credits_used': 11380.0, 'entitlement': 12600, 'observed_at': '2026-11-20T22:00:00Z', 'login': 'Faraclas'},
            {'date': '2026-11-21', 'credits_used': 11796.365, 'entitlement': 12600, 'observed_at': '2026-11-21T14:52:00Z', 'login': 'Faraclas'},
        ],
        'error': None,
    }
    usage_payload = {
        'schema_version': 1, 'status': 'fresh', 'source': 'github.copilot_internal.user',
        'observed_at': '2026-11-21T14:52:00Z', 'last_attempt_at': '2026-11-21T14:52:00Z',
        'next_refresh_after': '2026-11-21T14:53:00Z', 'error': None,
        'account': {'login': 'Faraclas', 'plan': 'enterprise'},
        'premium': {
            'used_percentage': 93.62, 'percent_remaining': 6.38, 'entitlement': 12600,
            'quota_remaining': 803.635, 'credits_used': 11796.365, 'unlimited': False,
            'overage_permitted': True, 'token_based_billing': True,
        },
        'reset_at': '2026-12-01T00:00:00Z',
        'nominal_usd': {'basis': '1 credit = $0.01 consumption value; not an invoice', 'entitlement': 126.0, 'quota_remaining': 8.0364},
    }
    page = ui['new_page'](viewport={'width': width, 'height': height}, color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
    page.route('**/api/cost-breakdown', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(cost_payload),
    ))
    page.route('**/api/extensions/copilot-credits/history*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(history_payload),
    ))
    page.route('**/api/extensions/copilot-credits/usage*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(usage_payload),
    ))
    page.goto(ui['url'] + '/')
    page.wait_for_selector('#page-chat')
    if width < 720:
        page.locator('.page.active [data-mobile-nav-toggle]:visible').click()
    page.locator('[data-nav-page="dashboard"]').click()
    page.locator('[data-dashboard-tab="costs"]').click()
    page.wait_for_function("document.querySelectorAll('#cost-spend-svg .costs-spend-line').length === 3")

    # Default unit is Copilot credits; chart sits below the breakdown tables.
    assert page.locator('#cost-spend-unit-credits').get_attribute('aria-pressed') == 'true'
    assert page.locator('#cost-spend-unit-usd').get_attribute('aria-pressed') == 'false'
    assert 'cr' in page.locator('#cost-spend-val-viper').inner_text()
    assert 'cr' in page.locator('#cost-spend-val-total').inner_text()
    assert 'cr' in page.locator('#cost-spend-val-others').inner_text()
    assert '12:00 AM America/New_York' in page.locator('#cost-spend-note').inner_text()
    assert 'penny-accurate invoice' in page.locator('#cost-spend-note').inner_text()

    # Three distinct Viper avatar palette stroke colors and zero inline style attributes inside the card.
    strokes = page.evaluate("""() => ['viper', 'total', 'others'].map((key) =>
        getComputedStyle(document.querySelector(`#cost-spend-svg .costs-spend-line--${key}`)).stroke
    )""")
    assert len(set(strokes)) == 3, (theme, strokes)
    inline_styles = page.locator('#cost-monthly-spend [style]').count()
    assert inline_styles == 0, f'expected zero inline style attributes, got {inline_styles}'

    # Scroll containment on #page-costs > .costs-scroll.
    scroller = page.locator('#page-costs > .costs-scroll')
    assert scroller.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')

    shots = os.environ.get('OUROBOROS_UI_EVIDENCE_DIR')
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.locator('#cost-monthly-spend').scroll_into_view_if_needed()
        page.screenshot(path=str(Path(shots) / f'monthly-spend-credits-{theme}-{width}.png'))

    # Toggle to USD ($ at 1 credit = $0.01) and inspect semantic data table.
    page.locator('#cost-spend-unit-usd').click()
    assert page.locator('#cost-spend-unit-usd').get_attribute('aria-pressed') == 'true'
    assert page.locator('#cost-spend-val-viper').inner_text() == '$32.11'
    assert page.locator('#cost-spend-val-total').inner_text() == '$117.96'
    assert page.locator('#cost-spend-val-others').inner_text() == '$85.86'

    page.locator('#cost-spend-table-summary').click()
    rows = page.locator('#cost-monthly-spend-table tbody tr')
    assert rows.count() == len(viper_days)
    # Nov 6 has Viper data ($2.40) and no invented Total/Everyone else snapshots ('—').
    first_cells = rows.nth(0).locator('td').all_inner_texts()
    assert first_cells == ['$2.40', '—', '—'], first_cells
    # Nov 21 has all three values.
    last_cells = rows.nth(len(viper_days) - 1).locator('td').all_inner_texts()
    assert last_cells == ['$32.11', '$117.96', '$85.86'], last_cells
    assert scroller.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    if shots and width >= 1360:
        page.locator('#cost-monthly-spend').scroll_into_view_if_needed()
        page.screenshot(path=str(Path(shots) / f'monthly-spend-usd-table-{theme}-{width}.png'))
    assert not ui['posts'], 'the view must not save settings or send messages'
