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
