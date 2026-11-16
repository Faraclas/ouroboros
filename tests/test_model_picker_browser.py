"""Room model browsing over disposable backend data; provider traffic is fenced."""
import json
import os
import pathlib

import pytest
from ouroboros import config
from tests.test_room_model_browser import model_server, launch_model_browser  # noqa: F401

pytestmark = pytest.mark.ui_browser

CATALOG = {'read_state': 'ok', 'sources_read_state': 'ok', 'model_sources': [], 'items': [
    {'id': name, 'value': f'openai::{name}'} for name in ['alpha-model', 'initial', 'omega-model']
] + [{'id': 'other-model', 'value': 'anthropic::other-model'}]}


@pytest.mark.parametrize('theme,width', [('dark', 1360), ('dark', 390), ('light', 1360), ('light', 390)])
def test_room_model_browse_search_dismiss_and_provider_drafts(model_server, tmp_path, theme, width):
    from playwright.sync_api import sync_playwright, expect
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, _ = model_server
    settings = json.loads(config.SETTINGS_PATH.read_text())
    settings['ANTHROPIC_API_KEY'] = 'fixture-only'
    config.SETTINGS_PATH.write_text(json.dumps(settings))
    writes = []
    with sync_playwright() as pw:
        browser = launch_model_browser(pw)
        context = browser.new_context(viewport={'width': width, 'height': 1000})
        context.add_init_script(f"localStorage.setItem('ouroboros.theme','{theme}')")
        page = context.new_page(); errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def guard(route):
            path = route.request.url.split(url)[-1]
            if route.request.method != 'GET':
                writes.append(path); route.fulfill(json={}); return
            if path.startswith('/api/model-catalog'):
                route.fulfill(json=CATALOG)
            elif path == '/' or path.startswith('/static/') or path.startswith('/api/projects') or path == '/api/settings' or path.startswith('/api/owner/primary-model'):
                route.continue_()
            else:
                route.fulfill(json={})
        context.route('**/*', guard)
        page.goto(url)
        page.locator('[data-pm-row="alpha"]').click()
        dashboard = page.locator('[data-pm-detail] .room-model-editor')
        out = pathlib.Path(os.environ.get('ROOM_MODEL_SCREENSHOTS', str(tmp_path)))
        out.mkdir(parents=True, exist_ok=True)

        def browse(editor, label):
            field = editor.locator('[data-model-role-model]')
            expect(field).to_have_value('initial')
            assert field.evaluate('n=>n.readOnly')
            expect(editor.locator('[data-model-search]')).not_to_be_checked()
            editor.scroll_into_view_if_needed()
            field.click()
            options = page.locator('.ui-model-options:not([hidden]) [data-model-value]')
            expect(options).to_have_count(3)
            expect(page.locator('.ui-model-options:not([hidden]) [aria-selected="true"]')).to_have_text('initial')
            assert page.locator('.ui-model-options:not([hidden])').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
            page.evaluate("async () => { await Promise.all(document.getAnimations().filter(a=>a.effect?.target?.closest?.('.ui-popup')).map(a=>a.finished)); }")
            page.screenshot(path=str(out / f'{label}-{theme}-{width}.png'))
            field.press('ArrowDown'); field.press('Enter')
            expect(field).to_have_value('omega-model')
            expect(editor.locator('[data-room-model-apply]')).to_be_enabled()
            editor.locator('[data-model-search]').check()
            assert not field.evaluate('n=>n.readOnly')
            field.fill('alpha')
            expect(options).to_have_count(1)
            editor.locator('[data-model-search]').uncheck()
            field.click(); expect(options).to_have_count(3)
            provider = editor.locator('[data-model-role-source]')
            assert provider.get_attribute('aria-label') == 'Primary model provider'
            assert provider.locator('option:not([disabled])').evaluate_all('ns=>ns.map(n=>n.value)') == ['api:openai', 'api:anthropic']
            assert 'Add a key' not in provider.inner_text()
            provider.select_option('api:anthropic')
            expect(field).to_have_value('')
            expect(editor.locator('[data-room-model-apply]')).to_be_disabled()
            field.click()
            expect(options).to_have_count(1)
            expect(options).to_have_text(['other-model'])
            options.click()
            expect(field).to_have_value('other-model')
            expect(editor.locator('[data-room-model-apply]')).to_be_enabled()
            assert 'Local uses the built-in local runtime on this machine.' in editor.inner_text()
            assert editor.evaluate('n=>n.scrollWidth<=n.clientWidth+1')
            editor.get_by_role('button', name='Cancel', exact=True).click()

        browse(dashboard, 'dashboard')
        expect(dashboard.locator('[data-model-role-model]')).to_have_value('initial')
        page.evaluate('window.mountComposer("alpha")')
        capsule = page.locator('#fixture-composer .chat-model-mode button')
        expect(capsule).to_have_text('initial')
        capsule.click()
        popup = page.locator('.room-model-popup:not([hidden])')
        browse(popup, 'chat-popup')
        expect(popup).to_have_count(0)
        for dismiss in ['escape', 'outside']:
            capsule.click()
            field = popup.locator('[data-model-role-model]')
            expect(field).to_have_value('initial')
            field.press('ArrowDown'); field.press('Enter')
            expect(field).to_have_value('omega-model')
            if dismiss == 'escape':
                field.click(); field.press('Escape')
            else:
                page.locator('#chat-input').click()
            expect(popup).to_have_count(0)
        capsule.click()
        expect(popup.locator('[data-model-role-model]')).to_have_value('initial')
        page.keyboard.press('Escape')
        expect(page.locator('#chat-input')).to_have_value('Keep this unsent draft')
        assert not writes
        assert not errors, errors
        context.close(); browser.close()


def test_provider_change_refreshes_catalog_without_saving(model_server):
    from playwright.sync_api import sync_playwright, expect
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, _ = model_server
    settings = json.loads(config.SETTINGS_PATH.read_text())
    settings['ANTHROPIC_API_KEY'] = 'fixture-only'
    config.SETTINGS_PATH.write_text(json.dumps(settings))
    reads, writes = [], []
    with sync_playwright() as pw:
        browser = launch_model_browser(pw)
        page = browser.new_page()
        def guard(route):
            path = route.request.url.split(url)[-1]
            if route.request.method != 'GET':
                writes.append(path); route.fulfill(json={})
            elif path.startswith('/api/model-catalog'):
                reads.append(path)
                items = CATALOG['items'] if len(reads) == 1 else [
                    {'id': 'newly-listed', 'value': 'anthropic::newly-listed'}]
                route.fulfill(json={**CATALOG, 'items': items})
            elif path == '/' or path.startswith('/static/') or path.startswith('/api/projects') or path == '/api/settings' or path.startswith('/api/owner/primary-model'):
                route.continue_()
            else: route.fulfill(json={})
        page.route('**/*', guard)
        page.goto(url); page.locator('[data-pm-row="alpha"]').click()
        editor = page.locator('[data-pm-detail] .room-model-editor')
        field = editor.locator('[data-model-role-model]')
        field.click()
        expect(page.locator('.ui-model-options:not([hidden]) [data-model-value]')).to_have_count(3)
        editor.locator('[data-model-role-source]').select_option('api:anthropic')
        field.click()
        expect(page.locator('.ui-model-options:not([hidden]) [data-model-value]')).to_have_text(['newly-listed'])
        assert len(reads) == 2 and not writes
        assert field.input_value() == ''
        browser.close()


def test_shared_nonroom_chooser_remains_editable(model_server):
    from playwright.sync_api import sync_playwright, expect
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, _ = model_server
    with sync_playwright() as pw:
        browser = launch_model_browser(pw)
        page = browser.new_page()
        page.route('**/*', lambda route: route.continue_() if route.request.method == 'GET' and (route.request.url == url + '/' or route.request.url.startswith(url + '/static/')) else route.fulfill(json={}))
        page.goto(url)
        page.evaluate('''async () => {
            const {modelChooserHtml,bindModelChoosers}=await import('/static/modules/model_chooser.js');
            const host=document.createElement('section'); host.id='nonroom';
            host.innerHTML=modelChooserHtml('aria-label="Settings model"','initial','nonroom-models',['initial','another']);
            document.body.append(host);window.disposeChooser=bindModelChoosers(host);
        }''')
        field = page.get_by_role('combobox', name='Settings model')
        assert not field.evaluate('n=>n.readOnly')
        field.fill('arbitrary-model-id')
        expect(field).to_have_value('arbitrary-model-id')
        expect(page.locator('#nonroom-models')).to_contain_text('You can keep this model ID')
        page.evaluate('window.disposeChooser();document.querySelector("#nonroom").remove()')
        assert page.locator('#nonroom-models').count() == 0
        # A long room catalog opens at the saved entry, not at the top or a filtered singleton.
        page.evaluate('''async () => {
            const {modelChooserHtml,bindModelChoosers,updateModelChooser,setModelChooserSearch}=await import('/static/modules/model_chooser.js');
            const host=document.createElement('section'); host.id='long-room';
            host.innerHTML=modelChooserHtml('data-model-browse aria-label="Room model"','model-70','long-models',Array.from({length:100},(_,i)=>`model-${i}`));
            document.body.append(host);window.disposeChooser=bindModelChoosers(host);
            window.refreshChooser=()=>updateModelChooser(host.querySelector('input'),Array.from({length:101},(_,i)=>`model-${i}`));
            window.searchChooser=enabled=>setModelChooserSearch(host.querySelector('input'),enabled);
        }''')
        room = page.get_by_role('combobox', name='Room model')
        room.click()
        expect(page.locator('#long-models [role="option"]')).to_have_count(100)
        expect(page.locator('#long-models [aria-selected="true"]')).to_have_text('model-70')
        assert page.locator('#long-models').evaluate('n=>n.scrollTop>0 && (()=>{const a=n.querySelector("[aria-selected=true]").getBoundingClientRect(),b=n.getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom;})()')
        room.press('ArrowUp'); room.press('Enter'); expect(room).to_have_value('model-69')
        page.evaluate('window.searchChooser(true)')
        room.fill('model-100')
        handle = room.element_handle()
        page.evaluate('window.refreshChooser()')
        assert handle.evaluate('n=>n.isConnected && n.value') == 'model-100'
        expect(page.locator('#long-models [role="option"]')).to_have_count(1)
        page.evaluate('window.searchChooser(false)')
        expect(page.locator('#long-models [role="option"]')).to_have_count(101)
        page.evaluate('window.disposeChooser();document.querySelector("#long-room").remove()')
        assert page.locator('#long-models').count() == 0
        browser.close()


def test_active_subscription_choices_and_stale_api_refresh(model_server):
    """Use actual role editor with account facts; discovery never writes the draft."""
    from playwright.sync_api import sync_playwright, expect
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1': pytest.skip('UI smoke opt-in')
    url, _ = model_server
    with sync_playwright() as pw:
        browser = launch_model_browser(pw)
        page = browser.new_page()
        page.route('**/*', lambda route: route.continue_() if route.request.method == 'GET' and (route.request.url == url + '/' or route.request.url.startswith(url + '/static/')) else route.fulfill(json={}))
        page.goto(url)
        page.evaluate('''async () => {
            const {createModelRolesEditor}=await import('/static/modules/model_roles.js');
            const host=document.createElement('section');host.id='account-picker';document.querySelector('#content').replaceChildren(host);
            window.pickerStore={accountsKnown:true,snapshot:{profiles:{profiles:[
                {profile:{harness_id:'live-harness',profile_id:'enabled',enabled:true},status:{availability:'exhausted'}},
                {profile:{harness_id:'live-harness',profile_id:'disabled',enabled:false}},
                {profile:{harness_id:'off-harness',profile_id:'off',enabled:false}}
            ]}}};
            window.pickerSources=[{id:'live-source',credentialHarness:'live-harness'},{id:'off-source',credentialHarness:'off-harness'}];
            window.pendingReads=[];
            window.editor=createModelRolesEditor({hostId:host.id,browseModels:true,showContext:false,store:window.pickerStore,
                catalogFetch:()=>new Promise(resolve=>window.pendingReads.push(resolve))});
            window.pickerLoad=(model='openai::initial')=>window.editor.load({OUROBOROS_MODEL:model,OPENAI_API_KEY:'fixture',ANTHROPIC_API_KEY:'fixture'},
                {modelSlots:[{slot:'main',settingKey:'OUROBOROS_MODEL',inputId:'account-model',label:'Primary model'}]});
            window.pickerLoad();window.editor.adoptCatalog({items:[],model_sources:window.pickerSources});
        }''')
        provider = page.locator('#account-picker [data-model-role-source]')
        enabled = lambda: provider.locator('option:not([disabled])').evaluate_all('ns=>ns.map(n=>n.value)')
        assert enabled() == ['subscription:live-source', 'api:openai', 'api:anthropic']
        provider.select_option('subscription:live-source')
        pins = page.locator('#account-picker [data-model-role-account]')
        assert pins.locator('option[value="disabled"]').is_disabled()
        assert not pins.locator('option[value="enabled"]').is_disabled()
        # Exhausted quota is not a disabled account. A saved unavailable route stays identifiable.
        page.evaluate("window.pickerLoad('claudexor::off-source=saved');window.editor.adoptCatalog({items:[],model_sources:window.pickerSources})")
        assert provider.input_value() == 'subscription:off-source'
        assert provider.locator('option:checked').evaluate('n=>n.disabled'), provider.evaluate('n=>n.outerHTML')
        page.evaluate('window.subscriptionReads=window.pendingReads;window.pendingReads=[]')
        provider.select_option('api:anthropic')
        provider.select_option('api:openai')
        page.wait_for_function('window.pendingReads.length===2')
        page.evaluate("window.pendingReads[1]({items:[{value:'openai::fresh'}],model_sources:window.pickerSources})")
        field = page.locator('#account-model');field.click()
        expect(page.locator('.ui-model-options:not([hidden]) [data-model-value]')).to_have_text(['fresh'])
        # An older complete/error response cannot overwrite a newer catalog or select a model.
        page.evaluate("window.pendingReads[0]({items:[{value:'openai::stale'}],errors:[{error:'old failure'}],model_sources:[]})")
        expect(page.locator('.ui-model-options:not([hidden]) [data-model-value]')).to_have_text(['fresh'])
        assert field.input_value() == '' and provider.input_value() == 'api:openai'
        page.evaluate("window.editor.destroy();for(const resolve of window.subscriptionReads)resolve({items:[{value:'claudexor::live-source=late'}]})")
        assert page.locator('.ui-model-options:not([hidden])').count() == 0
        browser.close()
