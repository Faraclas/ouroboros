"""Produced recovery facts reach Project history without changing card/unread semantics."""
from types import SimpleNamespace

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401
from ouroboros.test_evidence import diagnostic_directory

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def test_saved_work_notice_uses_project_history_and_sidebar_unread(direct_server_with_data, monkeypatch):
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros import pause_notices
    from supervisor import message_bus
    from supervisor.restart_retention import hold_stopped_queue

    fixture = direct_server_with_data
    root = fixture["data_dir"]
    project = create_project(root, "recovery", name="Recovery example")
    task = {"id": "saved-work", "chat_id": project["chat_id"], "project_id": project["id"],
            "root_task_id": "saved-work", "type": "task"}
    write_task_result(root, task["id"], "scheduled", chat_id=project["chat_id"], project_id=project["id"])
    hold_stopped_queue(task, root, "app_stop")
    with monkeypatch.context() as mp:
        mp.setattr(message_bus, "DATA_DIR", root)
        mp.setattr(message_bus, "load_state", lambda: {"owner_id": 1})
        mp.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(send_message=lambda *a, **k: None))
        pause_notices.reconcile_pause_notices(root)
    out = diagnostic_directory(root.parent, "recovery-notice")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for theme in ("dark", "light"):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.add_init_script(f"localStorage.setItem('ouroboros.theme', '{theme}')")
                page.goto(fixture["url"], wait_until="domcontentloaded")
                row = page.locator('.nav-project-row[data-project-id="recovery"]')
                row.wait_for(state="visible")
                # DESIGN Unread: host facts placed in a task card do not count as a new reply.
                expect(row.locator(".nav-project-unread-own")).to_be_hidden()
                row.click()
                panel = page.locator("#project-panel")
                expect(panel).to_contain_text("The app stopped with this work saved.")
                expect(panel).to_contain_text("Use Resume on this task when available; existing holds still apply.")
                expect(row).to_have_attribute("data-unread-count", "0")
                assert not page.locator("#chat-messages").get_by_text("The app stopped with this work saved.", exact=False).count()
                # Text can exist while the flyout is still sliding offscreen.
                page.wait_for_function("""() => {
                    const panel = document.querySelector('#project-panel');
                    const r = panel?.getBoundingClientRect();
                    return r && r.left >= 0 && r.right <= innerWidth + 1 &&
                        !document.getAnimations().some(a => a.playState === 'running' &&
                            Number.isFinite(a.effect?.getComputedTiming().endTime));
                }""")
                page.screenshot(path=str(out / f"recovery-{theme}.png"), full_page=True)
                context.close()
        finally:
            browser.close()
