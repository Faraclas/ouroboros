"""Production room editors and routes over disposable data; no model generation."""
from __future__ import annotations

import json
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

from ouroboros import config, projects_registry as projects
from ouroboros.gateway.router import collect_routes

pytestmark = pytest.mark.ui_browser


@pytest.fixture
def model_server(tmp_path, monkeypatch):
    import uvicorn
    repo = pathlib.Path(__file__).parents[1]
    data = tmp_path / 'data'; data.mkdir()
    monkeypatch.setattr(config, 'DATA_DIR', data)
    monkeypatch.setattr(config, 'SETTINGS_PATH', data / 'settings.json')
    settings = {'OUROBOROS_MODEL': 'openai::initial', 'OPENAI_API_KEY': 'fixture-only',
                'OUROBOROS_RUNTIME_MODE': 'pro', 'OUROBOROS_STARTER_SKILLS': '[]'}
    config.SETTINGS_PATH.write_text(json.dumps(settings), encoding='utf-8')
    for key, value in settings.items(): monkeypatch.setenv(key, value)
    projects.create_project(data, 'alpha', name='Model selection project')
    projects.create_project(data, 'beta', name='Independent project')
    boot = '''<script type="module">
import {initProjectsManager} from '/static/modules/projects_manager.js';
import {composerMarkup,createComposer} from '/static/modules/composer.js';
import {apiClient,apiFetch} from '/static/modules/api_client.js';
const mount=document.createElement('section'); mount.id='dashboard-panel-projects';
document.getElementById('content').append(mount);
window.manager=initProjectsManager({mount,state:{activePage:'dashboard',dashboardActiveSubtab:'projects'},onChanged:async()=>{}});
window.mountComposer=async (projectId='alpha')=>{
 window.controls?.destroy(); document.querySelector('#fixture-composer')?.remove();
 document.getElementById('primary-sidebar')?.setAttribute('hidden','');
 const dock=document.createElement('div'); dock.id='fixture-composer'; dock.className='chat-input-wrap'; dock.innerHTML=composerMarkup(); document.body.append(dock);
 const byId=s=>dock.querySelector('#chat-'+s);
 window.sentFrames=[];
 window.controls=createComposer({page:dock,byId,projectId,chatId:projectId?1001:1,isMain:!projectId,
 ws:{send(frame){window.sentFrames.push(frame);return {status:'sent'};}},replies:{frameField:()=>({})},
 chatSessionId:'fixture',storeKey:s=>projectId+s,refreshState:async()=>{},onLayout:()=>{},onSent:()=>null,readingBack:()=>false});
 byId('input').value='Keep this unsent draft';
};
</script>'''
    html = (repo / 'web/index.html').read_text(encoding='utf-8').replace('<script type="module" src="/static/app.js"></script>', boot)
    async def index(request):
        return HTMLResponse((repo / 'web/index.html').read_text(encoding='utf-8') if request.query_params.get('normal') == '1' else html)
    app = Starlette(routes=[Route('/', index), *collect_routes(data_dir=data), Mount('/static', StaticFiles(directory=repo / 'web'))])
    app.state.drive_root = data; app.state.repo_dir = repo
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.01)
    assert server.started
    try: yield f'http://127.0.0.1:{port}', data
    finally:
        server.should_exit = True; thread.join(10); sock.close(); assert not thread.is_alive()


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('light', 390)])
def test_shared_project_editors_conflict_refresh_and_order(model_server, theme, width, tmp_path):
    from playwright.sync_api import sync_playwright
    import httpx
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, data = model_server
    writes = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects/') or path.startswith('/api/owner/primary-model') or path == '/api/settings':
                if route.request.method != 'GET': writes.append((path, route.request.post_data_json))
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        editor = page.locator('[data-pm-detail] .room-model-editor')
        field = editor.locator('[data-model-role-model]'); field.wait_for()
        assert field.input_value() == 'initial'
        field.fill('dashboard-choice'); editor.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-detail] .room-model-editor').textContent.includes('Primary saved')")
        assert projects.get_project(data, 'alpha')['cognition']['primary_model']['model'] == 'openai::dashboard-choice'
        assert projects.get_project(data, 'beta')['cognition']['primary_model']['model'] == 'openai::initial'
        page.evaluate('window.mountComposer("alpha")')
        capsule = page.locator('#fixture-composer .chat-model-mode')
        page.wait_for_function("document.querySelector('#fixture-composer .chat-model-mode').textContent.includes('dashboard-choice')")
        assert page.locator('#fixture-composer .chat-toolbar-row').evaluate("n=>[...n.children].filter(x=>x.classList.contains('chat-capsule')).map(x=>x.classList.contains('chat-model-mode')?'model':x.classList.contains('chat-context-mode')?'context':x.classList.contains('chat-effort-range')?'effort':'subagents')") == ['model','context','effort','subagents']
        capsule.get_by_role('button').click()
        popup = page.locator('.room-model-popup:not([hidden])')
        popup.locator('[data-model-role-model]').fill('composer-choice')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Primary saved')")
        assert page.locator('#fixture-composer #chat-input').input_value() == 'Keep this unsent draft'
        assert projects.get_project(data, 'alpha')['cognition']['primary_model']['model'] == 'openai::composer-choice'
        page.keyboard.press('Escape')
        field.fill('stale-editor-choice'); editor.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('[data-pm-detail] .room-model-editor').textContent.includes('not confirmed')")
        assert field.input_value() == 'stale-editor-choice'
        assert editor.locator('[data-room-model-apply]').is_disabled()
        editor.locator('[data-room-model-check]').click()
        page.wait_for_function("document.querySelector('[data-pm-detail] .room-model-editor').textContent.includes('Current model checked')")
        editor.get_by_role('button', name='Cancel draft').click()
        assert field.input_value() == 'composer-choice'
        # Ordinary refresh preserves this editor's input node and unsaved draft.
        field.fill('kept-draft'); handle = field.element_handle()
        page.get_by_role('button', name='Refresh', exact=True).click()
        page.wait_for_function("!document.querySelector('[data-pm-detail]').hasAttribute('aria-busy')")
        assert handle.evaluate('n=>n.isConnected && n.value') == 'kept-draft'
        editor.get_by_role('button', name='Cancel draft').click()
        editor.scroll_into_view_if_needed()
        out = pathlib.Path(os.environ.get('ROOM_MODEL_SCREENSHOTS', str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f'room-model-{theme}-{width}.png'))
        assert page.locator('[data-pm-detail]').evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        assert len(writes) == 3  # successful Dashboard + composer, then genuine stale CAS refusal
        assert httpx.get(url + '/api/owner/primary-model?project_id=alpha').json()['primary_model']['model'] == 'openai::composer-choice'
        assert not errors
        context.close(); browser.close()


def test_real_composer_waits_for_save_and_recovers_lost_response(model_server):
    """The actual Send path, not a surrogate pendingSave invocation; transport is inert."""
    from playwright.sync_api import sync_playwright
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, data = model_server
    held = []; posts = []; lose = [False]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1360, 'height': 1000})
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path.startswith('/api/owner/primary-model') and route.request.method == 'POST':
                posts.append(route.request.post_data_json)
                if lose[0]:
                    route.fetch(); route.abort(); return
                held.append(route); return
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects') or path == '/api/settings' or path.startswith('/api/owner/primary-model'):
                route.continue_()
            else: route.fulfill(status=200, json={})
        context.route('**/*', guard)
        page = context.new_page(); page.goto(url); page.wait_for_function('!!window.mountComposer')
        page.evaluate('window.mountComposer("alpha")')
        capsule = page.locator('#fixture-composer .chat-model-mode button')
        page.wait_for_function("document.querySelector('.chat-model-mode').textContent.includes('initial')")
        capsule.click(); popup = page.locator('.room-model-popup:not([hidden])')
        popup.locator('[data-model-role-model]').fill('pending-choice')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('[data-room-model-apply]').textContent==='Applying…'")
        page.locator('#chat-send').click()
        page.wait_for_function("document.querySelector('#chat-send').disabled")
        assert page.evaluate('window.sentFrames.length') == 0
        assert len(held) == 1
        held.pop().continue_()
        page.wait_for_function('window.sentFrames.length===1')
        assert projects.get_project(data, 'alpha')['cognition']['primary_model']['model'] == 'openai::pending-choice'
        assert page.evaluate('window.sentFrames[0].project_id') == 'alpha'
        assert page.evaluate('window.sentFrames[0].content') == 'Keep this unsent draft'
        # A committed save with a lost reply must not send or automatically re-submit it.
        lose[0] = True
        capsule.click()  # Send is outside the chooser and correctly closed it.
        popup.locator('[data-model-role-model]').fill('lost-reply-choice')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('not confirmed')")
        page.locator('#chat-input').fill('Kept after failure')
        page.locator('#chat-send').click()
        assert page.evaluate('window.sentFrames.length') == 1
        assert page.locator('#chat-input').input_value() == 'Kept after failure'
        assert len(posts) == 2
        assert projects.get_project(data, 'alpha')['cognition']['primary_model']['model'] == 'openai::lost-reply-choice'
        capsule.click()
        popup.locator('[data-room-model-check]').click()
        page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Current model checked')")
        page.locator('#chat-send').click(); page.wait_for_function('window.sentFrames.length===2')
        assert len(posts) == 2
        # Main edits global primary; existing Project bindings remain independent.
        page.keyboard.press('Escape'); page.evaluate('window.mountComposer("")')
        page.wait_for_function("document.querySelector('.chat-model-mode').textContent.includes('initial')")
        page.locator('#fixture-composer .chat-model-mode button').click()
        popup = page.locator('.room-model-popup:not([hidden])')
        assert 'global primary' in popup.inner_text()
        lose[0] = False
        popup.locator('[data-model-role-model]').fill('unknown-future-model')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('[data-room-model-apply]').textContent==='Applying…'")
        assert len(held) == 1; held.pop().continue_()
        page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Primary saved')")
        assert json.loads(config.SETTINGS_PATH.read_text())['OUROBOROS_MODEL'] == 'openai::unknown-future-model'
        assert projects.get_project(data, 'alpha')['cognition']['primary_model']['model'] == 'openai::lost-reply-choice'
        assert projects.get_project(data, 'beta')['cognition']['primary_model']['model'] == 'openai::initial'
        page.evaluate('window.controls.destroy()')
        assert page.locator('.room-model-popup').count() == 0
        context.close(); browser.close()


@pytest.mark.parametrize('case', ['settings-draft', 'read-only-failure', 'initial-stale', 'local-pin'])
def test_model_read_freshness_and_local_intent(model_server, case):
    from playwright.sync_api import sync_playwright
    from ouroboros.composer_cognition import update_project_cognition
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, data = model_server
    if case == 'local-pin':
        update_project_cognition(data, 'alpha', primary_model={'model':'claudexor::codex=unknown-model', 'use_local':False, 'credential_profile_id':'saved-profile'})
    primary_reads = [0]; held = []; delay_settings = [False]; fail_read = [False]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1360,'height':1000})
        def guard(route):
            path = route.request.url.split(url)[-1]
            if path.startswith('/api/owner/primary-model') and route.request.method == 'GET':
                primary_reads[0] += 1
                if fail_read[0]: route.abort(); return
                if case == 'initial-stale' and primary_reads[0] == 1:
                    held.append((route,route.fetch())); return
            if path == '/api/settings' and delay_settings[0]:
                held.append((route,route.fetch())); return
            if path == '/' or path.startswith('/static/') or path.startswith('/api/projects') or path == '/api/settings' or path.startswith('/api/owner/primary-model'):
                route.continue_()
            else: route.fulfill(status=200,json={})
        context.route('**/*',guard)
        page=context.new_page(); page.goto(url); page.wait_for_function('!!window.mountComposer'); page.evaluate('window.mountComposer("alpha")')
        capsule=page.locator('#fixture-composer .chat-model-mode button')
        if case != 'initial-stale': page.wait_for_function("document.querySelector('.chat-model-mode').dataset.known==='true'")
        if case == 'read-only-failure': fail_read[0]=True
        capsule.click(); popup=page.locator('.room-model-popup:not([hidden])')
        if case == 'read-only-failure':
            page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Could not read')")
            page.locator('#chat-send').click()
            assert page.evaluate('window.sentFrames.length') == 1
        else:
            field=popup.locator('[data-model-role-model]'); field.wait_for()
            if case == 'settings-draft':
                delay_settings[0]=True; popup.locator('[data-room-model-check]').click()
                page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Reading primary')")
                page.wait_for_timeout(50)  # let the intercepted settings request reach its explicit barrier
                assert len(held)==1
                field.fill('draft-during-settings')
                route,response=held.pop(); route.fulfill(response=response)
                page.wait_for_function("!document.querySelector('.room-model-popup').textContent.includes('Reading primary')")
                assert field.input_value() == 'draft-during-settings'
            elif case == 'initial-stale':
                field.fill('new-confirmed'); popup.locator('[data-room-model-apply]').click()
                page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Primary saved')")
                assert len(held)==1
                route,response=held.pop(); route.fulfill(response=response)
                page.wait_for_timeout(50)
                assert 'new-confirmed' in capsule.inner_text()
                assert field.input_value()=='new-confirmed'
            else:
                popup.locator('[data-model-local]').check(); popup.locator('[data-room-model-apply]').click()
                page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Primary saved') || document.querySelector('.room-model-popup').textContent.includes('not confirmed')")
                assert projects.get_project(data,'alpha')['cognition']['primary_model']=={'model':'claudexor::codex=unknown-model','use_local':True,'credential_profile_id':''}
        context.close(); browser.close()


def test_normal_spa_main_and_project_choosers(model_server, tmp_path):
    """Unmodified app.js mounts real Main/Project composers; state/history/socket are fixtures."""
    from playwright.sync_api import sync_playwright
    from ouroboros.composer_cognition import main_cognition
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url,data=model_server
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1360,'height':1000})
        context.add_init_script("localStorage.setItem('ouroboros.theme','dark')")
        context.route_web_socket('**/*',lambda ws:None)
        def guard(route):
            path=route.request.url.split(url)[-1]
            if path.startswith('/api/state'):
                rows=[projects.get_project(data,key) for key in ('alpha','beta')]
                route.fulfill(status=200,json={'version':'7.9.1','supervisor_ready':True,'boot':{'phase':'ready','dialogue_ready':True,'tasks_ready':True},'projects':rows,'active_chat_activities':[], 'works':[], 'rooms':[], 'works_complete':True,**main_cognition()}); return
            if path.startswith('/api/health'):
                route.fulfill(status=200,json={'version':'7.9.1','boot':{'phase':'ready','dialogue_ready':True,'tasks_ready':True}}); return
            if path.startswith('/api/onboarding'): route.fulfill(status=204); return
            if path == '/' or path.startswith('/?normal') or path.startswith('/static/') or path.startswith('/api/projects') or path=='/api/settings' or path.startswith('/api/owner/primary-model'):
                route.continue_()
            elif path.startswith('/api/chat/history'): route.fulfill(status=200,json={'messages':[], 'has_more':False,'window':{'complete':True},'coverage':{'complete':True}})
            else: route.fulfill(status=200,json={})
        context.route('**/*',guard)
        page=context.new_page(); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(url+'/?normal=1')
        main=page.locator('#page-chat .chat-model-mode button')
        main.wait_for(); page.wait_for_function("document.querySelector('#page-chat .chat-model-mode').dataset.known==='true'")
        main.click(); popup=page.locator('.room-model-popup:not([hidden])')
        popup.locator('[data-model-role-model]').fill('normal-main')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("document.querySelector('.room-model-popup').textContent.includes('Primary saved')")
        page.keyboard.press('Escape')
        page.get_by_text('Model selection project',exact=True).first.click()
        project=page.locator('.project-panel .chat-model-mode button')
        project.wait_for();project.click();popup=page.locator('.room-model-popup:not([hidden])')
        assert popup.locator('[data-model-role-model]').input_value()=='initial'
        popup.locator('[data-model-role-model]').fill('normal-project')
        popup.locator('[data-room-model-apply]').click()
        page.wait_for_function("[...document.querySelectorAll('.room-model-popup:not([hidden])')].some(n=>n.textContent.includes('Primary saved'))")
        assert projects.get_project(data,'alpha')['cognition']['primary_model']['model']=='openai::normal-project'
        assert json.loads(config.SETTINGS_PATH.read_text())['OUROBOROS_MODEL']=='openai::normal-main'
        out=pathlib.Path(os.environ.get('ROOM_MODEL_SCREENSHOTS',str(tmp_path)));out.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(out/'room-model-normal-spa.png'))
        assert not errors,errors
        context.close();browser.close()
