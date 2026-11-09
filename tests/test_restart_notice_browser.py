"""The actual deferred-input producer renders its acknowledgement in the real SPA."""
from __future__ import annotations

import pytest

from tests.test_agent_restart_intent import runtime, accept  # noqa: F401
from tests.test_human_rows_browser import _host, _serve_history, _push, _evidence, ROOM
from tests.test_sidebar_activity_browser import mount_sidebar
from tests.test_subscription_setup_browser import subscription_ui  # noqa: F401
from tests.ui_chat_viewport_smoke import wait_drawer_closed

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("theme,width", [("light", 1360), ("dark", 1360), ("light", 390)])
def test_saved_restart_notice_is_readable_without_claiming_execution(subscription_ui, runtime, monkeypatch, tmp_path, theme, width):
    from supervisor import message_bus, worker_chat_lane

    ui, out = subscription_ui, _evidence(tmp_path)
    page = ui["page"]
    _bridge, frames = _host(runtime.root, monkeypatch)
    _serve_history(page, runtime.root)
    mounted = mount_sidebar(ui, width, theme, False, ROOM, [])
    if width < 700:
        page.locator('#primary-sidebar [data-nav-page="chat"]').click()
        wait_drawer_closed(page)
    accept(runtime)
    monkeypatch.setattr(runtime.workers, "_get_chat_agent", lambda: pytest.fail("must remain deferred"))
    monkeypatch.setattr(runtime.workers, "send_with_budget", message_bus.send_with_budget)
    worker_chat_lane.handle_chat_direct(1, "A later ordinary question", None, task_metadata={"client_message_id": "saved-visual"})
    from tests.system_e2e.harness import wait_until
    assert wait_until(lambda: [f for f in frames if f.get("system_type") == "restart_notice"], 10)
    _push(mounted, frames)
    notice = page.get_by_text("Saved for execution after Restart or release of the pending restart.", exact=False)
    notice.wait_for()
    assert "Stop control" in notice.inner_text()
    box = notice.bounding_box()
    assert box and box["width"] > 100 and box["x"] >= 0 and box["x"] + box["width"] <= width
    page.screenshot(path=str(out / f"saved-restart-{theme}-{width}.png"), animations="disabled")
    assert ui["errors"] == []
