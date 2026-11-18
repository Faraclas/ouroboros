"""Real Close/cleanup flow, with production routes against disposable files only."""
from __future__ import annotations

import os
import pathlib
import socket
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from ouroboros import projects_registry as r, project_footprint as f
from ouroboros.gateway.router import collect_routes

pytestmark = pytest.mark.ui_browser


@pytest.fixture
def cleanup_server(tmp_path, monkeypatch):
    import uvicorn
    from supervisor import queue_transitions as transitions
    # No production queue participates in a disposable registry's Close.
    monkeypatch.setattr(transitions, '_live_project_task_ids', lambda *args, **kw: set())
    repo = pathlib.Path(__file__).parents[1]
    data = tmp_path / 'data'; material = tmp_path / 'project-temp'; material.mkdir()
    (material / 'unique-work').write_text('Disposable browser fixture')
    r.create_project(data, 'alpha', name='Cleanup delivery fixture')
    f.record_workspace_creation(data, 'alpha', str(material), 'fixture-base')
    r.create_project(data, 'viper-interface', name='Protected habitat')
    boot = '''<script type="module">
import {initProjectsManager} from '/static/modules/projects_manager.js';
import {initDashboard} from '/static/modules/dashboard.js';
const state={activePage:'dashboard',dashboardActiveSubtab:'projects'};
const dashboard=initDashboard({state}); dashboard.page.classList.add('active');
window.manager=initProjectsManager({mount:document.getElementById('dashboard-panel-projects'),state});
</script>'''
    html = (repo / 'web/index.html').read_text().replace('<script type="module" src="/static/app.js"></script>', boot)
    async def index(request): return HTMLResponse(html)
    app = Starlette(routes=[Route('/', index), *collect_routes(data_dir=data), Mount('/static', StaticFiles(directory=repo / 'web'))])
    app.state.drive_root = data; app.state.repo_dir = repo
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.01)
    assert server.started
    try: yield f'http://127.0.0.1:{port}', data, material
    finally:
        server.should_exit = True; thread.join(10); sock.close(); assert not thread.is_alive()
        deadline = time.monotonic() + 5
        while transitions._PROJECT_DELETE_WORKERS and time.monotonic() < deadline: time.sleep(.01)
        assert not transitions._PROJECT_DELETE_WORKERS


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_close_extra_confirmation_and_server_cleanup(cleanup_server, theme, width, tmp_path):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright
    url, data, material = cleanup_server
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard); context.route_web_socket('**/*', lambda ws: None)
        page.goto(url); page.locator('[data-pm-row="viper-interface"]').click()
        assert not page.get_by_role('button', name='Close project…', exact=True).count()
        page.locator('[data-pm-row="alpha"]').click(); page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog'); choice = dialog.get_by_label('Cleanup choice')
        assert choice.input_value() == 'complete'
        assert choice.locator('option').all_text_contents() == ['Close and clean up completely', 'Close, keep it reopen-friendly', 'Close only (keep everything, as today)']
        remove = dialog.get_by_role('checkbox', name=f'Remove {material}', exact=True); remove.wait_for(); assert remove.is_checked()
        close = dialog.get_by_role('button', name='Close project', exact=True); assert close.is_disabled()
        dialog.get_by_role('checkbox', name=f'Confirm loss of local material at {material}', exact=True).check()
        assert close.is_enabled()
        # Escape restores the initiating control; changing preset fetches a fresh plan.
        page.keyboard.press('Escape'); assert page.get_by_role('button', name='Close project…', exact=True).evaluate('(n)=>n===document.activeElement')
        page.get_by_role('button', name='Close project…', exact=True).click(); dialog = page.get_by_role('dialog')
        dialog.get_by_label('Cleanup choice').select_option('none'); assert dialog.get_by_role('button', name='Close project', exact=True).is_enabled()
        dialog.get_by_label('Cleanup choice').select_option('reopen_friendly')
        dialog.get_by_role('checkbox', name=f'Remove {material}', exact=True).wait_for()
        assert not dialog.get_by_role('checkbox', name=f'Remove {material}', exact=True).is_checked()
        dialog.get_by_label('Cleanup choice').select_option('complete')
        confirm = dialog.get_by_role('checkbox', name=f'Confirm loss of local material at {material}', exact=True); confirm.wait_for(); confirm.check()
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'cleanup-{theme}-{width}.png'))
        # Remove the browser immediately after the acknowledged Close: server owns cleanup.
        with page.expect_response(lambda response: response.url.endswith('/alpha/delete')) as response:
            dialog.get_by_role('button', name='Close project', exact=True).click()
        assert response.value.ok and response.value.json()['folder_untouched'] is False
        page.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            project = r.get_reserved_project(data, 'alpha')
            if project.get('cleanup_request', {}).get('status') == 'finished': break
            time.sleep(.02)
        assert project['lifecycle'] == 'tombstoned' and project['cleanup_request']['status'] == 'finished', project
        assert not material.exists()
        assert project['cleanup_request']['result']['results'][0]['status'] == 'removed'
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e))); page.goto(url)
        page.evaluate("window.manager.select('alpha')")
        page.get_by_role('button', name='Clean up local material…', exact=True).wait_for()
        page.get_by_text('Cleanup and reconstruction history', exact=True).click()
        page.get_by_text('missing (cleaned', exact=False).wait_for()
        assert not material.exists()  # viewing or reopening never reconstructs silently
        assert not errors
        context.close(); browser.close()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_retained_reason_and_cancel_saved_cleanup(cleanup_server, theme, width, tmp_path, monkeypatch):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright
    from ouroboros import project_cleanup_context as custody
    url, data, material = cleanup_server
    original = custody.context
    def live(*args):
        facts = original(*args); facts['processes'] = True; return facts
    monkeypatch.setattr(custody, 'context', live)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard); context.route_web_socket('**/*', lambda ws: None)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_text('Work or an owned process is still active.', exact=False).wait_for()
        assert dialog.get_by_role('checkbox', name=f'Remove {material}', exact=True).is_disabled()
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'cleanup-retained-{theme}-{width}.png'))
        page.keyboard.press('Escape')
        r.begin_project_deletion(data, 'alpha', cleanup_request={'mode': 'complete', 'selections': [], 'confirmations': {}, 'system_repo_dir': str(pathlib.Path(__file__).parents[1])})
        r.complete_project_deletion(data, 'alpha')
        page.goto(url); page.evaluate("window.manager.select('alpha')")
        cancel = page.get_by_role('button', name='Cancel saved cleanup', exact=True); cancel.wait_for(); cancel.click()
        page.get_by_text('Saved cleanup cancelled.', exact=False).wait_for()
        assert r.get_reserved_project(data, 'alpha')['cleanup_request']['status'] == 'cancelled'
        assert material.exists() and not errors
        page.goto(url); page.evaluate("window.manager.select('alpha')")
        page.get_by_text('Saved cleanup cancelled.', exact=False).wait_for()
        assert not page.get_by_text('Cleanup failed:', exact=False).count()
        page.screenshot(path=str(out / f'cleanup-cancelled-{theme}-{width}.png'))
        context.close(); browser.close()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX native byte names')
@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_native_name_preview_selection_and_exact_cleanup(cleanup_server, theme, width, tmp_path):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright, expect
    from tests.test_project_native_names import native_targets
    from ouroboros.task_results import write_task_result
    from ouroboros.utils import json_dumps
    url, data, material = cleanup_server
    paths = native_targets(material.parent)
    for path in paths: f.record_workspace_creation(data, 'alpha', str(path), 'fixture-base')
    write_task_result(data, 'old-proof', 'completed', project_id='alpha', artifacts=[{'path': str(paths[0] / 'keep')}])
    display = lambda p: json_dumps(str(p), ensure_ascii=False)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard); context.route_web_socket('**/*', lambda ws: None)
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        page.get_by_text('Cleanup preview — read only', exact=True).click()
        preview = page.locator('.pm-resource-preview')
        expect(preview.get_by_text(display(paths[0]), exact=True).first).to_be_visible()
        page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Close project', exact=True)
        expect(dialog.get_by_text(display(paths[0]), exact=True)).to_be_visible()
        expect(dialog.get_by_text(display(paths[3]), exact=True)).to_be_visible()
        # Escape checks the dialog itself, not unrelated occurrences of its title.
        page.keyboard.press('Escape'); expect(dialog).to_have_count(0)
        page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Close project', exact=True)
        dialog.get_by_role('checkbox', name=f'Remove {material}', exact=True).wait_for()
        # Retained generated evidence inside raw_ff protects its ancestor. Choose
        # raw_fe instead; all Unicode/escaped-looking siblings must survive.
        for checkbox in dialog.locator('input[type="checkbox"]').all():
            if checkbox.is_checked() and checkbox.is_enabled(): checkbox.uncheck()
        selected = paths[1]
        dialog.get_by_role('checkbox', name=f'Remove {display(selected)}', exact=True).check()
        dialog.get_by_role('checkbox', name=f'Confirm loss of local material at {display(selected)}', exact=True).check()
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'native-cleanup-{theme}-{width}.png'))
        with page.expect_response(lambda response: response.url.endswith('/alpha/delete')) as response:
            dialog.get_by_role('button', name='Close project', exact=True).click()
        assert response.value.ok
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            project = r.get_reserved_project(data, 'alpha')
            if project.get('cleanup_request', {}).get('status') == 'finished': break
            time.sleep(.02)
        assert project['cleanup_request']['status'] == 'finished', project
        assert not selected.exists() and all((p / 'keep').exists() for p in paths if p != selected)
        assert material.exists() and not errors
        context.close(); browser.close()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_paged_cleanup_preserves_choices_and_never_selects_unseen(cleanup_server, theme, width, tmp_path):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright
    url, data, material = cleanup_server
    for i in range(31):
        path = tmp_path / f'retained-{i:02}'; path.mkdir()
        f.record_target(data, 'alpha', str(path), task_id='fixture', origin='pre_existing')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard); context.route_web_socket('**/*', lambda ws: None)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog'); confirm = dialog.get_by_label(f'Confirm loss of local material at {material}', exact=True)
        confirm.wait_for(); confirm.check()
        assert dialog.locator('.pm-resource').count() == 25
        dialog.get_by_role('button', name='Next', exact=True).click()
        dialog.get_by_text('26–32 of 32 matching · 32 total', exact=True).wait_for()
        assert dialog.locator('.pm-resource').count() == 7
        assert dialog.get_by_role('button', name='Close project', exact=True).is_enabled()
        # A non-default existing target on page two is an explicit new selection.
        choice = dialog.get_by_label(f'Remove {tmp_path / "retained-30"}', exact=True); choice.check()
        dialog.get_by_label(f'Confirm loss of local material at {tmp_path / "retained-30"}', exact=True).check()
        dialog.get_by_role('button', name='Previous', exact=True).click(); confirm.wait_for(); assert confirm.is_checked()
        assert dialog.locator('.pm-resource').count() == 25
        out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'cleanup-pages-{theme}-{width}.png'))
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        assert dialog.evaluate('(n)=>n.contains(document.activeElement)'), 'paging must retain dialog keyboard ownership'
        page.keyboard.press('Escape'); dialog.wait_for(state='detached')
        assert page.get_by_role('dialog').count() == 0
        assert page.get_by_role('button', name='Close project…', exact=True).evaluate('(n)=>n===document.activeElement')
        assert material.exists() and (tmp_path / 'retained-30').exists()
        assert r.get_reserved_project(data, 'alpha')['lifecycle'] == 'active'
        assert not errors
        browser.close()
