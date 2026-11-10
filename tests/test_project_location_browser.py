"""Real Projects manager and production HTTP owners over disposable registry data."""
from __future__ import annotations

import os
import pathlib
import socket
import threading

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from ouroboros import projects_registry as r
from ouroboros.gateway.router import collect_routes

pytestmark = pytest.mark.ui_browser


@pytest.fixture
def location_server(tmp_path, monkeypatch):
    import uvicorn
    import ouroboros.gateway.projects as gateway
    repo = pathlib.Path(__file__).parents[1]
    data = tmp_path / 'data'; a = tmp_path / 'old'; b = tmp_path / 'replacement-checkout-with-a-long-descriptive-name'
    a.mkdir(); b.mkdir()
    r.create_project(data, 'alpha', name='A retained room with an unavailable old location', working_dir=str(a))
    r.create_project(data, 'beta', name='Another independent room')
    monkeypatch.setattr(gateway, '_broadcast_projects_changed', lambda *args: None)
    boot = '''<script type="module">
import {initProjectsManager} from '/static/modules/projects_manager.js';
const mount=document.createElement('section'); mount.id='dashboard-panel-projects'; document.getElementById('content').append(mount);
window.manager=initProjectsManager({mount,state:{activePage:'dashboard',dashboardActiveSubtab:'projects'},onChanged:async()=>{}});
</script>'''
    html = (repo / 'web/index.html').read_text(encoding='utf-8').replace('<script type="module" src="/static/app.js"></script>', boot)
    async def index(request): return HTMLResponse(html)
    app = Starlette(routes=[Route('/', index), *collect_routes(data_dir=data), Mount('/static', StaticFiles(directory=repo / 'web'))])
    app.state.drive_root = data; app.state.repo_dir = repo
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    import time
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.01)
    assert server.started
    try: yield f'http://127.0.0.1:{port}', data, a, b
    finally:
        server.should_exit = True; thread.join(10); sock.close(); assert not thread.is_alive()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_real_location_editor_save_cancel_conflict_and_unknown(location_server, theme, width, tmp_path):
    from playwright.sync_api import sync_playwright
    import httpx
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, data, old, target = location_server
    writes = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path.startswith('/static/') or path == '/' or path.startswith('/api/projects/') or path.startswith('/api/fs/dirs'):
                if route.request.method != 'GET': writes.append((path, route.request.post_data_json))
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        location = page.locator('[data-pm-location]')
        location.locator('[data-pm-location-edit]').click()
        location.locator('[data-pm-location-mode]').select_option('folder')
        field = location.locator('[data-pm-location-path]'); field.fill(str(target))
        # Ordinary navigation retains the actual draft node/value.
        page.locator('[data-pm-row="beta"]').click(); page.locator('[data-pm-row="alpha"]').click()
        assert field.input_value() == str(target)
        location.locator('[data-pm-location-cancel]').click()
        assert not location.locator('.pm-location-editor').is_visible()
        assert r.get_project(data, 'alpha')['working_dir'] == str(old) and not writes
        location.locator('[data-pm-location-edit]').click(); field.fill(str(target))
        location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('saved')")
        assert r.get_project(data, 'alpha')['working_dir'] == str(target)
        assert r.get_project(data, 'beta')['working_dir'] == ''
        # Two editors use the exact same HTTP basis; no synthetic conflict reply.
        location.locator('[data-pm-location-edit]').click()
        current = httpx.get(url + '/api/projects/alpha/detail').json()['project']
        winner = httpx.post(url + '/api/projects/alpha/update', json={'working_dir': str(old), 'expected_location': current['location']})
        assert winner.status_code == 200
        location.locator('[data-pm-location-mode]').select_option('none')
        location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('changed')")
        assert location.locator('[data-pm-location-apply]').is_disabled()
        location.locator('[data-pm-location-check]').click()
        location.locator('[data-pm-location-continue]').wait_for(state='visible')
        location.locator('[data-pm-location-continue]').click()
        location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('saved')")
        assert r.get_project(data, 'alpha')['working_dir'] == ''
        assert r.get_project(data, 'alpha')['resources'][0]['path'] == str(old)
        # Lost successful response: real server commits, client receives transport failure.
        def lose(route):
            if route.request.method == 'POST': route.fetch(); route.abort('failed')
            else: route.continue_()
        context.route('**/api/projects/alpha/update', lose)
        location.locator('[data-pm-location-edit]').click(); location.locator('[data-pm-location-mode]').select_option('folder'); field.fill(str(target))
        location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('not confirmed')")
        assert location.locator('[data-pm-location-apply]').is_disabled()
        location.locator('[data-pm-location-check]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('matches')")
        assert r.get_project(data, 'alpha')['working_dir'] == str(target)
        assert field.input_value() == str(target)
        # Inspect actual manager/editor at natural size, with a genuine missing-path refusal.
        context.unroute('**/api/projects/alpha/update', lose)
        field.fill(str(target / 'absent')); location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('Not applied')")
        location.scroll_into_view_if_needed()
        out = pathlib.Path(os.environ.get('LOCATION_SCREENSHOTS', str(tmp_path)))
        out.mkdir(parents=True, exist_ok=True); page.screenshot(path=str(out / f'location-{theme}-{width}.png'))
        assert page.locator('[data-pm-detail]').evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        assert not errors
        # A failed send before commit must not mistake a legacy empty default for
        # the owner's explicit folderless choice.
        r.create_project(data, 'legacy', name='Legacy fileless room')
        page.get_by_role('button', name='Refresh', exact=True).click()
        page.locator('[data-pm-row="legacy"]').click()
        location.locator('[data-pm-location-edit]').click()
        def unsent(route): route.abort('failed')
        context.route('**/api/projects/legacy/update', unsent)
        location.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-location-status]').textContent.includes('not confirmed')")
        location.locator('[data-pm-location-check]').click()
        page.wait_for_function("!document.querySelector('[data-pm-location-check]').disabled")
        assert 'No retry is needed' not in location.locator('[data-pm-location-status]').inner_text()
        assert location.locator('[data-pm-location-apply]').is_enabled()
        assert r.get_project(data, 'legacy').get('working_dir_configured') is not True
        # A caller's asynchronous refresh may deliberately focus another field.
        # The location component must not take it back on successful completion.
        page.evaluate('''async () => {
            const {createLocationSection} = await import('/static/modules/project_location.js');
            const project = (await (await fetch('/api/projects/alpha/detail')).json()).project;
            const outside = document.createElement('input'); outside.id='focus-outside'; document.body.append(outside);
            const section = createLocationSection({project, signal:new AbortController().signal,
                isCurrent:()=>true, onApplied:async()=>{outside.focus();}});
            section.root.id='focus-location'; document.getElementById('content').append(section.root);
        }''')
        focus_section = page.locator('#focus-location')
        focus_section.locator('[data-pm-location-edit]').click()
        focus_section.locator('[data-pm-location-mode]').select_option('none')
        focus_section.locator('[data-pm-location-apply]').click()
        page.wait_for_function("document.querySelector('#focus-location [data-pm-location-status]').textContent.includes('saved')")
        assert page.evaluate("document.activeElement.id") == 'focus-outside'
        context.close(); browser.close()
