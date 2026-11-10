"""Production detail GET and manager in a disposable registry; preview never posts."""
from __future__ import annotations

import asyncio
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
from ouroboros import projects_registry as r, project_management as m
from ouroboros.gateway.projects import api_project_detail
from ouroboros.gateway.router import collect_routes

pytestmark = pytest.mark.ui_browser


@pytest.fixture
def preview_server(tmp_path):
    import uvicorn
    repo = pathlib.Path(__file__).parents[1]
    data, ordinary, habitat = tmp_path / 'data', tmp_path / 'external', tmp_path / 'habitat'
    ordinary.mkdir(); habitat.mkdir()
    r.create_project(data, 'alpha', name='Ordinary resource inspection', working_dir=str(ordinary))
    r.create_project(data, 'beta', name='Conversation only')
    r.create_project(data, 'viper-interface', name='Viper Interface', working_dir=str(habitat))
    alias = tmp_path / 'habitat-alias'; alias.symlink_to(habitat, target_is_directory=True)
    m.add_project_resource(data, 'alpha', str(alias))
    m.add_project_resource(data, 'alpha', str(tmp_path / 'missing'))
    control = {'hold': False, 'entered': False, 'error': False, 'preview_reads': 0, 'completed': 0}
    async def detail(request):
        if request.query_params.get('resource_preview') == '1':
            control['preview_reads'] += 1; control['entered'] = True
            while control['hold']: await asyncio.sleep(.01)
            if control['error']:
                from starlette.responses import JSONResponse
                return JSONResponse({'error': 'Injected inspection failure'}, status_code=503)
        result = await api_project_detail(request)
        if request.query_params.get('resource_preview') == '1': control['completed'] += 1
        return result
    boot = '''<script type="module">
import {initProjectsManager} from '/static/modules/projects_manager.js';
import {initDashboard} from '/static/modules/dashboard.js';
import {initStorage} from '/static/modules/storage.js';
const state={activePage:'dashboard',dashboardActiveSubtab:'projects'};
const dashboard=initDashboard({state}); dashboard.page.classList.add('active');
window.manager=initProjectsManager({mount:document.getElementById('dashboard-panel-projects'),state,
    onStorage:()=>{dashboard.activateTab('storage'); document.getElementById('dashboard-tab-storage').focus();}});
window.storage=initStorage({state,mount:document.getElementById('dashboard-panel-storage')});
</script>'''
    html = (repo / 'web/index.html').read_text(encoding='utf-8').replace('<script type="module" src="/static/app.js"></script>', boot)
    async def index(request): return HTMLResponse(html)
    app = Starlette(routes=[Route('/', index), Route('/api/projects/{project_id}/detail', detail),
                            *collect_routes(data_dir=data), Mount('/static', StaticFiles(directory=repo / 'web'))])
    app.state.drive_root = data; app.state.repo_dir = repo
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.01)
    assert server.started
    def snapshot():
        return {str(p): p.read_bytes() for base in (data, ordinary, habitat) for p in base.rglob('*') if p.is_file()}
    before = snapshot()
    try: yield f'http://127.0.0.1:{port}', control, before
    finally:
        control['hold'] = False; server.should_exit = True; thread.join(10); sock.close(); assert not thread.is_alive()
        assert before == snapshot()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('dark', 390), ('light', 1360), ('light', 390)])
def test_preview_navigation_keyboard_error_cancel_dispose_and_purity(preview_server, theme, width, tmp_path):
    from playwright.sync_api import sync_playwright
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, control, _ = preview_server
    writes, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if route.request.method != 'GET': writes.append(path); route.abort(); return
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard)
        context.route_web_socket('**/*', lambda ws: None)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        section = page.locator('.pm-resource-preview'); summary = section.locator('summary')
        assert control['preview_reads'] == 0
        summary.focus(); page.keyboard.press('Enter')
        section.get_by_role('button', name='Refresh preview', exact=True).wait_for()
        text = section.inner_text()
        assert 'Excluded from generic cleanup' in text and 'Unique work: not inspected' in text
        assert 'Viper Interface' in text and 'missing' in text and 'installation-wide' in text
        assert section.get_by_role('button').count() == 2
        assert section.locator('a').count() == 0 and not writes
        section.scroll_into_view_if_needed()
        out = pathlib.Path(os.environ.get('PREVIEW_SCREENSHOTS', str(tmp_path / 'screenshots')))
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'preview-{theme}-{width}.png'))
        assert section.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        # Error is local/recoverable, no mutation or hidden global preview job.
        control['error'] = True
        section.get_by_role('button', name='Refresh preview', exact=True).focus()
        page.keyboard.press('Enter')
        assert summary.evaluate('(n)=>n===document.activeElement')
        section.get_by_role('alert').wait_for(); assert 'No cleanup was requested' in section.inner_text()
        control['error'] = False; section.get_by_role('button', name='Retry preview').click()
        section.get_by_role('button', name='Refresh preview', exact=True).wait_for()
        # Delayed real GET can be closed without late paint or focus theft.
        control['hold'] = True; control['entered'] = False
        section.get_by_role('button', name='Refresh preview', exact=True).click()
        section.get_by_role('button', name='Cancel inspection').click()
        assert not section.evaluate('(n)=>n.open')
        control['hold'] = False
        page.locator('[data-pm-row="beta"]').click()
        page.locator('.pm-resource-preview summary').click()
        page.get_by_text('No resource or starting folder is configured.', exact=False).wait_for()
        page.locator('[data-pm-row="viper-interface"]').click()
        count = control['preview_reads']; page.locator('.pm-resource-preview summary').click()
        page.get_by_text('This habitat project is excluded', exact=False).wait_for()
        assert control['preview_reads'] == count
        page.locator('[data-pm-row="alpha"]').click(); page.locator('.pm-resource-preview summary').click()
        page.get_by_role('button', name='Open installation-wide Storage').wait_for()
        page.get_by_role('button', name='Open installation-wide Storage').click()
        assert page.locator('#dashboard-panel-storage').is_visible()
        assert page.locator('#dashboard-tab-storage').evaluate('(n)=>n===document.activeElement')
        page.locator('#dashboard-tab-projects').click()
        page.locator('.pm-resource-preview summary').click()
        page.get_by_role('button', name='Refresh preview', exact=True).wait_for()
        # Destroy while a held request is outstanding; completion must touch no detached UI.
        control['hold'] = True
        page.get_by_role('button', name='Refresh preview', exact=True).click()
        page.get_by_role('button', name='Cancel inspection').wait_for()
        completed = control['completed']
        page.evaluate('window.manager.destroy()'); control['hold'] = False
        deadline = time.monotonic() + 5
        while control['completed'] <= completed and time.monotonic() < deadline: time.sleep(.01)
        assert control['completed'] > completed
        assert not page.locator('.projects-manager').count()
        assert not writes and not errors
        context.close(); browser.close()
