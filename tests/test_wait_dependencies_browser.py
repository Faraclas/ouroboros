"""Real model-tool sleep producers, canonical store/API and normal SPA multi-room navigation.

The local model is scripted; no provider or owner installation data is used. Roots
are admitted through the public API, not injected into the census or rendered labels.
"""
from __future__ import annotations

import json
import os
import threading
from urllib.error import HTTPError

import pytest

from tests import test_ui_smoke_playwright as smoke
from tests.test_room_work_backend_browser import _http, _wait, _reply, _user_text, _open_chat, Evidence
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET
from tests.ui_console_errors import PageErrors

direct_server_with_data = smoke.direct_server_with_data
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


class ChainModel:
    def __init__(self, monkeypatch):
        from tests import fixtures_mock_llm
        self.targets = {}
        self.calls = {}
        self.lock = threading.Lock()
        monkeypatch.setattr(fixtures_mock_llm._Handler, "do_POST", lambda handler: self.handle(handler))

    def handle(self, handler):
        payload = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))))
        text = "\n".join(_user_text(message) for message in payload.get("messages", []) if message.get("role") == "user")
        marker = next((name for name in ("WAIT-CHAIN-A", "WAIT-CHAIN-B", "WAIT-CHAIN-C") if name in text), "")
        message, finish = {"role": "assistant", "content": "OK"}, "stop"
        if not payload.get('tools') and payload.get('response_format', {}).get('type') == 'json_object':
            message['content'] = json.dumps({'status': 'SAFE', 'reason': 'Scripted local verification operation'})
        if marker and payload.get("tools"):
            with self.lock:
                ordinal = self.calls[marker] = self.calls.get(marker, 0) + 1
            if marker.endswith('B') and ordinal == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [{"id": "call-service", "type": "function",
                    "function": {"name": "start_service", "arguments": json.dumps({"name": "chain-tests",
                        "cwd": "task_drive", "cmd": ["python3", "-c", "import time; time.sleep(120)"]})}}]}
                finish = "tool_calls"
            elif ordinal == (2 if marker.endswith('B') else 1):
                if marker.endswith("C"):
                    name, arguments = "escalate", {"question": "Which evidence should release this test chain?", "wait_for_answer": True,
                        "options": [{"label": "Primary evidence"}, {"label": "Replication"}]}
                else:
                    target = self.targets[marker]
                    name, arguments = "await_messages", {"mode": "warm", "senders": [target], "tasks": [target], "dependency": {
                        "waiting_for": "Verified upstream result", "why": "Only the next installation operation waits",
                        "release": "The upstream owner reports its checked result", "on": [target]}}
                message = {"role": "assistant", "content": "", "tool_calls": [{"id": f"call-{marker}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                finish = "tool_calls"
        _reply(handler, payload, message, finish)


def _work(url, task_id):
    return next((work for work in _http(url, "GET", "/api/state").get("works", [])
                 if task_id in work.get("members", {}).get("ids", [])), None)


def _launch(pw, *, width, theme):
    browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("OUROBOROS_UI_BROWSER_EXECUTABLE") or None)
    page = browser.new_page(viewport={"width": width, "height": 920})
    page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
    page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
    return browser, page, {"pageerrors": PageErrors(page, "chromium")}


@pytest.mark.parametrize(("theme", "width"), [("dark", 1360), ("light", 390)])
def test_sleep_chain_opens_target_status_and_returns(direct_server_with_data, tmp_path, monkeypatch, theme, width):
    from playwright.sync_api import sync_playwright
    model = ChainModel(monkeypatch)
    server = direct_server_with_data
    url, data = server["url"], server["data_dir"]
    rooms = []
    for name in ("Waiting installation", "Evidence decision"):
        folder = tmp_path / name.replace(" ", "-")
        folder.mkdir()
        rooms.append(_http(url, "POST", "/api/projects", {"name": name, "path": str(folder)})["project"])
    ids = []
    try:
        for marker, project in [("WAIT-CHAIN-C", rooms[1]), ("WAIT-CHAIN-B", None), ("WAIT-CHAIN-A", rooms[0])]:
            args = {"description": marker, "title": marker}
            if project:
                args["project_id"] = project["id"]
            else:
                args.update(source="web", chat_id=1)
            if ids:
                model.targets[marker] = ids[-1]
            response = _http(url, "POST", "/api/tasks", args)
            task_id = response["task_id"]
            ids.append(task_id)
            _wait(lambda: (work := _work(url, task_id)) and work["activity"] == "waiting", 60, marker + " sleeping")
        a, b, c = (_work(url, task_id) for task_id in reversed(ids))
        assert a["because"][0]["dependency"]["why"] == "Only the next installation operation waits"
        assert a["because"][0]["targets"][0]["work_id"] == b["work_id"]
        assert b["because"][0]["targets"][0]["work_id"] == c["work_id"]
        assert c["release"] == "owner"
        evidence = Evidence(data, f"wait-chain-{theme}-{width}")
        evidence.record("api", [a, b, c])
        tools = _http(url, 'GET', f'/api/logs/tools?task_id={ids[1]}&limit=100')
        evidence.record('service-tools', tools)
        _wait(lambda: any(tail.get('kind') == 'service' for tail in (_work(url, ids[1]) or {}).get('tails', [])),
              60, 'actual service observation published beside sleeping cognition')
        a, b, c = (_work(url, task_id) for task_id in reversed(ids))
        evidence.record('observed-api', [a, b, c])
        with sync_playwright() as pw:
            browser, page, log = _launch(pw, width=width, theme=theme)
            try:
                _open_chat(page, url)
                page.evaluate("p => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail:{project:p}}))", rooms[0])
                page.locator("#project-panel .rw-trigger").click()
                first = page.locator(f'#project-panel .rw-row[data-id="{a["work_id"]}"]')
                first.locator('.rw-dependencies').wait_for(state="visible")
                assert "Only the next installation" in first.inner_text()
                evidence.shot(page, "first-hop")
                first.get_by_role("button", name="Open in Main", exact=True).click()
                main = page.locator(f'#page-chat .rw-row[data-id="{b["work_id"]}"]')
                main.locator('.rw-dependencies').wait_for(state="visible")
                assert 'chain-tests' in main.inner_text()
                assert page.locator('#page-chat .rw-wait-back').is_visible()
                main.locator('[data-intent="open-project"]').click()
                last = page.locator(f'#project-panel .rw-row[data-id="{c["work_id"]}"]')
                last.wait_for(state="visible")
                assert "Waiting" in last.inner_text()
                assert last.locator('.rw-pill-question').is_visible(), 'the chain reaches the real owner choice'
                assert page.locator('#project-panel .rw-wait-back').is_visible()
                evidence.shot(page, "owner-decision")
                page.locator('#project-panel .rw-wait-back').get_by_role('button').click()
                main.locator('.rw-dependencies').wait_for(state="visible")
                page.locator('#page-chat .rw-wait-back').get_by_role('button').click()
                first.locator('.rw-dependencies').wait_for(state="visible")
                evidence.shot(page, "returned")
                # Ending the source wait withdraws its explanation, not a declaration
                # that its semantic prerequisite was satisfied. Upstream post-work
                # completion is a separate lifecycle, not this UI test's clock.
                _http(url, 'POST', f'/api/tasks/{ids[2]}/cancel', {'cascade': True})
                _wait(lambda: (current := _work(url, ids[2])) is None or
                      not any(reason.get('dependency') for reason in current.get('because', [])),
                      60, 'old sleep explanation withdrawn when its source ends')
                page.wait_for_function("() => ![...document.querySelectorAll('.rw-dependencies')].some(n => n.offsetWidth && n.textContent.includes('Only the next installation'))")
                evidence.shot(page, 'released')
                assert not log["pageerrors"].unexpected(), log["pageerrors"]
            finally:
                browser.close()
    finally:
        for task_id in ids:
            try:
                _http(url, "POST", f"/api/tasks/{task_id}/cancel", {"cascade": True})
            except HTTPError as exc:
                if exc.code != 404:
                    raise
