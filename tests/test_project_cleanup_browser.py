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


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_existing_task_workspace_cleanup_and_unavailable_page(cleanup_server, theme, width, tmp_path):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright, expect
    from ouroboros.task_results import write_task_result
    url, data, original = cleanup_server
    external = tmp_path / 'attached-external'; external.mkdir()
    (external / 'unique').write_text('deliberately selected disposable work')
    sibling = tmp_path / 'keep-sibling'; sibling.mkdir(); (sibling / 'keep').write_text('keep')
    r.create_project(data, 'beta', name='Existing external workspace', working_dir=str(external))
    retained = [data / f'retained-{i:02}' for i in range(27)]
    for path in retained: path.mkdir(); (path / 'evidence').write_text('durable')
    write_task_result(data, 'terminal-work', 'completed', project_id='beta', workspace_root=str(external),
                      artifacts=[{'path': str(p)} for p in retained])
    r.begin_project_deletion(data, 'beta'); r.complete_project_deletion(data, 'beta')
    out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
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
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e))); page.goto(url)
        page.evaluate("window.manager.select('beta')")
        entry = page.get_by_role('button', name='Clean up local material…', exact=True); entry.click()
        dialog = page.get_by_role('dialog', name='Clean up project', exact=True)
        remove = dialog.get_by_label(f'Remove {external}', exact=True)
        expect(remove).to_be_enabled(); expect(remove).not_to_be_checked()
        submit = dialog.get_by_role('button', name='Clean selected files', exact=True)
        expect(submit).to_be_disabled()
        search = dialog.get_by_label('Filter recorded paths')
        search.fill('retained-'); dialog.get_by_role('button', name='Find path', exact=True).click()
        expect(dialog.get_by_text('1–25 of 27 matching · 28 total', exact=True)).to_be_visible()
        expect(dialog.get_by_role('status')).to_contain_text('This page: 0 selectable, 25 unavailable.')
        expect(dialog.get_by_role('status')).to_contain_text('Nothing selectable on this page.')
        expect(submit).to_be_disabled()
        assert all(not c.is_enabled() for c in dialog.get_by_role('checkbox').all())
        page.screenshot(path=str(out / f'cleanup-unavailable-page-{theme}-{width}.png'))
        search.fill('no-matching-target'); dialog.get_by_role('button', name='Find path', exact=True).click()
        expect(dialog.get_by_role('status')).to_contain_text('No recorded paths match this filter.')
        expect(submit).to_be_disabled()
        dialog.get_by_label('Cleanup choice').select_option('none')
        expect(dialog.get_by_role('status')).to_contain_text('No cleanup will run.')
        expect(submit).to_be_disabled()
        dialog.get_by_label('Cleanup choice').select_option('complete')
        expect(remove).to_be_enabled()
        remove.check(); expect(submit).to_be_disabled()
        dialog.get_by_label(f'Confirm loss of local material at {external}', exact=True).check()
        expect(submit).to_be_enabled()
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        page.screenshot(path=str(out / f'cleanup-existing-workspace-{theme}-{width}.png'))
        with page.expect_response(lambda response: response.url.endswith('/beta/cleanup') and response.request.method == 'POST') as response:
            submit.click()
        assert response.value.ok and response.value.json()['results'][0]['status'] == 'removed'
        assert not external.exists() and (sibling / 'keep').read_text() == 'keep'
        assert all((p / 'evidence').read_text() == 'durable' for p in retained)
        assert original.exists() and not errors
        # A zero-selection Close is still useful, but must not queue no-op cleanup.
        page.evaluate("window.manager.select('alpha')")
        page.get_by_role('button', name='Close project…', exact=True).click()
        close_dialog = page.get_by_role('dialog', name='Close project', exact=True)
        original_choice = close_dialog.get_by_label(f'Remove {original}', exact=True)
        expect(original_choice).to_be_checked(); original_choice.uncheck()
        expect(close_dialog.get_by_role('status')).to_contain_text('closing will keep all local material')
        with page.expect_response(lambda response: response.url.endswith('/alpha/delete')) as closed:
            close_dialog.get_by_role('button', name='Close project', exact=True).click()
        assert closed.value.ok and 'cleanup' not in closed.value.request.post_data_json
        assert original.exists() and not r.get_reserved_project(data, 'alpha').get('cleanup_request')
        browser.close()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_project_wide_select_all_labels_and_absent_rows_without_paging(cleanup_server, theme, width, tmp_path):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright, expect
    from ouroboros.task_results import write_task_result
    url, data, material = cleanup_server
    import subprocess
    def init_clean_repo(path):
        path.mkdir(parents=True, exist_ok=True)
        remote = tmp_path / f'{path.name}-remote.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.email', 't@example.com'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'Test'], check=True, capture_output=True)
        (path / 'README').write_text('ok')
        subprocess.run(['git', '-C', str(path), 'add', 'README'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'commit', '-m', 'init'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'push', '-u', 'origin', 'HEAD'], check=True, capture_output=True)
    # Build a multi-page inventory (>25 targets) with protected, already gone,
    # attached external, and clean eligible targets on both page 1 and page 2.
    repo = pathlib.Path(__file__).parents[1]
    ext = tmp_path / 'ext-checkout'; init_clean_repo(ext); (ext / 'local.txt').write_text('keep')
    r.create_project(data, 'gamma', name='Project Attention like', working_dir=str(ext))
    clean_p1 = tmp_path / '00-clean-p1'; init_clean_repo(clean_p1)
    clean_p1_skip = tmp_path / '01-clean-p1-skip'; init_clean_repo(clean_p1_skip)
    gone_path = tmp_path / '02-already-gone'
    f.record_workspace_creation(data, 'gamma', str(clean_p1), 'fixture-base')
    f.record_workspace_creation(data, 'gamma', str(clean_p1_skip), 'fixture-base')
    f.record_workspace_creation(data, 'gamma', str(gone_path), 'fixture-base')
    # 24 protected artifacts + 1 shared repo = 25 protected, plus 2 clean eligible targets on page 2
    artifacts = [data / f'art-{i:02}' for i in range(24)]
    for p in artifacts: p.mkdir(); (p / 'ev').write_text('keep')
    write_task_result(data, 'gamma-work', 'completed', project_id='gamma', workspace_root=str(repo),
                      artifacts=[{'path': str(p)} for p in artifacts])
    clean_p2 = [tmp_path / f'zz-clean-p2-{i}' for i in range(2)]
    for p in clean_p2:
        init_clean_repo(p)
    f.record_target(data, 'gamma', str(repo), task_id='gamma-work', origin='pre_existing', note='Shared installation fixture')
    # Establish the page boundary in the recorded footprint itself. The same
    # artifact paths from the task result coalesce with these earlier entries.
    for p in artifacts:
        f.record_target(data, 'gamma', str(p), task_id='gamma-work', origin='pre_existing', note='Disposable browser fixture')
    for p in clean_p2:
        f.record_workspace_creation(data, 'gamma', str(p), 'fixture-base')
    out = pathlib.Path(os.environ.get('CLEANUP_SCREENSHOTS', str(tmp_path / 'screenshots'))); out.mkdir(parents=True, exist_ok=True)
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
        page.goto(url); page.evaluate("window.manager.select('gamma')")
        page.get_by_role('button', name='Close project…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Close project', exact=True)
        # Without paging: project-wide totals and all four status/reason labels are clear.
        status = dialog.get_by_role('status')
        expect(status).to_contain_text('Project-wide (31 total): 4 will remove · 1 needs confirmation · 25 protected · 1 already gone.')
        expect(dialog.get_by_text('Protected · Shared Viper installation, runtime, or protected habitat directory.', exact=True)).to_be_visible()
        expect(dialog.get_by_text('Already gone · path no longer exists on disk.', exact=True)).to_be_visible()
        expect(dialog.get_by_text('Needs confirmation · review local work below before removing.', exact=True)).to_be_visible()
        expect(dialog.get_by_text('Will remove · ready temporary material.', exact=False).first).to_be_visible()
        # Already-gone row has NO checkbox.
        assert dialog.get_by_role('checkbox', name=f'Remove {gone_path}', exact=True).count() == 0
        external = dialog.get_by_role('checkbox', name=f'Remove {ext}', exact=True)
        loss = dialog.get_by_role('checkbox', name=f'Confirm loss of local material at {ext}', exact=True)
        external.check(); loss.check()
        expect(dialog.get_by_text('Will remove · loss confirmed.', exact=True)).to_be_visible()
        (ext / 'new-local.txt').write_text('new local work after the first confirmation')
        path_filter = dialog.get_by_role('searchbox', name='Filter recorded paths')
        path_filter.fill('ext-checkout')
        dialog.get_by_role('button', name='Find path').click()
        expect(dialog.get_by_role('navigation', name='Cleanup target pages')).to_contain_text('1–1 of 1 matching')
        expect(external).to_be_checked()
        expect(loss).not_to_be_checked()
        expect(dialog.get_by_role('button', name='Close project', exact=True)).to_be_disabled()
        external.uncheck()
        path_filter.fill('')
        dialog.get_by_role('button', name='Find path').click()
        expect(dialog.get_by_role('navigation', name='Cleanup target pages')).to_contain_text('1–25 of 31 matching')
        # Top-level Select all eligible toggles all 4 eligible targets across pages.
        select_all = dialog.get_by_role('checkbox', name='Select all eligible across whole project (4 eligible)', exact=True)
        expect(select_all).to_be_checked()
        select_all.uncheck()
        expect(status).to_contain_text('Project-wide (31 total): 0 will remove')
        dialog.get_by_role('button', name='Next', exact=True).click()
        off_page = dialog.get_by_role('checkbox', name=f'Remove {clean_p2[0]}', exact=True)
        expect(off_page).to_be_visible(); expect(off_page).not_to_be_checked()
        expect(status).to_contain_text('Project-wide (31 total): 0 will remove')
        dialog.get_by_role('button', name='Previous', exact=True).click()
        expect(dialog.get_by_role('checkbox', name=f'Remove {clean_p1}', exact=True)).to_be_visible()
        select_all.check()
        expect(status).to_contain_text('Project-wide (31 total): 4 will remove')
        # Uncheck one row on page 1 -> 3 will remove across all pages (2 unseen on page 2).
        dialog.get_by_role('checkbox', name=f'Remove {clean_p1_skip}', exact=True).uncheck()
        expect(status).to_contain_text('Project-wide (31 total): 3 will remove')
        dialog.get_by_role('button', name='Next', exact=True).click()
        expect(dialog.get_by_role('checkbox', name=f'Remove {clean_p2[0]}', exact=True)).to_be_checked()
        dialog.get_by_role('button', name='Previous', exact=True).click()
        expect(dialog.get_by_role('checkbox', name=f'Remove {clean_p1_skip}', exact=True)).not_to_be_checked()
        assert dialog.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        page.screenshot(path=str(out / f'cleanup-project-wide-{theme}-{width}.png'))
        with page.expect_response(lambda response: response.url.endswith('/gamma/delete')) as resp:
            dialog.get_by_role('button', name='Close project', exact=True).click()
        assert resp.value.ok
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            project = r.get_reserved_project(data, 'gamma')
            if project.get('cleanup_request', {}).get('status') == 'finished': break
            time.sleep(.02)
        assert project['cleanup_request']['status'] == 'finished', project
        assert not clean_p1.exists()
        assert all(not p.exists() for p in clean_p2)  # unseen page 2 targets removed!
        assert clean_p1_skip.exists()  # excluded target preserved
        assert (ext / 'local.txt').read_text() == 'keep'  # unconfirmed external preserved
        assert all((p / 'ev').read_text() == 'keep' for p in artifacts)
        assert not errors
        context.close(); browser.close()
