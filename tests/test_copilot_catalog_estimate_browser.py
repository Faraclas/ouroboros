"""A fixture response reaches the real Logs SPA through worker and durable logs."""
from __future__ import annotations

import json
import queue
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.logs import api_logs_tail
from ouroboros.pricing import emit_llm_usage_event
from supervisor.events_budget import _handle_llm_usage
from tests.test_copilot_catalog_estimate import TARGET, USAGE, cached_catalog, normalized_usage
from tests.test_delegated_activity_browser import engine_ui
from tests.test_effort_logs_browser import _open_logs
from tests.test_subscription_setup_browser import capture, subscription_ui
from tests import test_subscription_role_routes_browser as roles

role_ui = roles.role_ui


@pytest.mark.ui_browser
@pytest.mark.serial
def test_catalog_estimate_in_real_logs_live_and_reloaded(engine_ui, cached_catalog, tmp_path):
    page = engine_ui["page"]
    page.set_viewport_size({"width": 390, "height": 844})
    sockets, published = [], []
    page.route_web_socket("**/ws", lambda socket: sockets.append(socket))
    context = SimpleNamespace(DRIVE_ROOT=tmp_path, bridge=SimpleNamespace(push_log=published.append),
                              RUNNING={key: {"task": {"id": key, "chat_id": 1, "type": "task"}}
                                       for key in ("catalog-complete", "catalog-incomplete")})
    app = Starlette(routes=[Route("/api/logs/{name}", api_logs_tail)])
    app.state.drive_root = tmp_path
    estimates = ["Copilot catalog estimate: 11.7 catalog units (not billed cost)",
                 "Copilot catalog estimate: unknown"]
    with TestClient(app) as client:
        def forward(route):
            parsed = urlparse(route.request.url)
            response = client.get(parsed.path + ("?" + parsed.query if parsed.query else ""))
            route.fulfill(status=response.status_code, content_type="application/json", body=response.content)

        page.route("**/api/logs/**", forward)
        page.goto(engine_ui["url"] + "/")
        page.wait_for_selector("#chat-messages", state="attached")
        for _ in range(100):
            if sockets:
                break
            page.wait_for_timeout(50)
        assert sockets
        for task, raw in [("catalog-complete", {**USAGE, "prompt_tokens": 273}),
                          ("catalog-incomplete", {**USAGE, "cached_tokens": None})]:
            usage = normalized_usage(usage=raw)
            worker = queue.Queue()
            emit_llm_usage_event(worker, task, TARGET["usage_model"], usage, usage["cost"])
            _handle_llm_usage(worker.get_nowait(), context)
        for event in published:
            assert event["cost"] == 0.125
            sockets[-1].send(json.dumps({"type": "log", "chat_id": 1, "data": event}))
        _open_logs(page)
        logs = page.locator("#log-entries")
        primary = logs.get_by_text(estimates[0])
        primary.wait_for()
        assert "Above the 272-token standard threshold" in primary.inner_text()
        assert "surcharge advertised" in primary.inner_text()
        assert "Token counts not fully reported" in logs.get_by_text(estimates[1]).inner_text()
        page.reload()
        page.wait_for_selector("#chat-messages", state="attached")
        _open_logs(page)
        primary.wait_for()
        assert logs.get_by_text(estimates[1]).count() == 1
        assert logs.evaluate("el => el.scrollWidth <= el.clientWidth")
        primary.scroll_into_view_if_needed()
        capture(page, "copilot-catalog-logs")


@pytest.mark.ui_browser
@pytest.mark.serial
@pytest.mark.parametrize("width", [390, 1360])
def test_standard_tier_setting_save_reload(role_ui, width):
    ui = role_ui
    roles.configure_mixed(ui)
    page = ui["page"]
    page.set_viewport_size({"width": width, "height": 900})
    roles.open_agents(ui)
    page.locator('[data-settings-tab="behavior"]').click()
    setting = page.locator("#s-copilot-prefer-standard-tier")
    setting.scroll_into_view_if_needed()
    assert not setting.is_checked()
    assert "soft target" in page.locator("#s-copilot-prefer-standard-tier-help").inner_text()
    capture(page, f"copilot-standard-tier-{width}")
    setting.check()
    with page.expect_response("**/api/settings"):
        page.locator("#btn-save-settings").click()
    assert ui["settings"]["OUROBOROS_COPILOT_PREFER_STANDARD_TIER"] == "true"
    page.reload()
    page.locator('[data-settings-tab="behavior"]').click()
    assert setting.is_checked()
    setting.uncheck()
    with page.expect_response("**/api/settings"):
        page.locator("#btn-save-settings").click()
    assert ui["settings"]["OUROBOROS_COPILOT_PREFER_STANDARD_TIER"] == "false"
    page.reload()
    page.locator('[data-settings-tab="behavior"]').click()
    assert not setting.is_checked()
