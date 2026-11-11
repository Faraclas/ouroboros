"""Work Items in the normal SPA; real disposable Project routes, controlled board/history/socket.

No model requests or owner data. Mutations use an in-memory receipt/CAS backend;
these tests judge the production navigation, board, composer and Reply owners.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from tests.test_room_model_browser import model_server  # noqa: F401

pytestmark = pytest.mark.ui_browser


@pytest.fixture
def navigation_ui(model_server):
    if os.environ.get('OUROBOROS_RUN_UI_SMOKE') != '1':
        pytest.skip('UI smoke opt-in')
    from playwright.sync_api import sync_playwright
    from ouroboros import projects_registry as projects
    from ouroboros.composer_cognition import main_cognition
    url, data = model_server
    rows = [projects.get_project(data, key) for key in ('alpha', 'beta')]
    items = {key: {'id': key, 'project_id': key, 'title': 'Discuss ' + key,
                  'description': 'Exact selected words α\nSecond line', 'outcome': '',
                  'revision': 1, 'disposition': 'idea', 'archived': False,
                  'references': [], 'prerequisites': []} for key in ('alpha', 'beta')}
    h = {'items': items, 'comments': {'alpha': [], 'beta': []}, 'writes': [],
         'receipts': {}, 'held': [], 'hold': '', 'lost': '', 'errors': [], 'frames': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('OUROBOROS_UI_BROWSER_EXECUTABLE') or None,
                                    args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1700, 'height': 1100})
        context.add_init_script("localStorage.setItem('ouroboros.theme','dark')")
        def socket(ws):
            ws.on_message(lambda message: h['frames'].append(message))
            ws.send(json.dumps({'type': 'heartbeat'}))
        context.route_web_socket('**/*', socket)
        def respond(route):
            req = route.request
            parsed = urlparse(req.url); path = parsed.path; q = parse_qs(parsed.query)
            if parsed.netloc != urlparse(url).netloc:
                route.abort(); return
            if path == '/' or path.startswith('/static/'):
                route.continue_(); return
            if path == '/api/state':
                route.fulfill(json={'version': '7.9.1', 'supervisor_ready': True,
                    'boot': {'phase': 'ready', 'dialogue_ready': True, 'tasks_ready': True},
                    'projects': rows, 'project_chat_ids': [r['chat_id'] for r in rows],
                    'active_chat_activities': [], 'works': [], 'rooms': [], 'works_complete': True,
                    **main_cognition()}); return
            if path == '/api/onboarding': route.fulfill(status=204); return
            if path == '/api/health': route.fulfill(json={'version': '7.9.1'}); return
            if path == '/api/chat/history':
                if h['hold'] == 'history':
                    h['held'].append(route); return
                chat = int(q.get('chat_id', ['1'])[0])
                route.fulfill(json={'messages': [{'role': 'assistant', 'text': 'Old unrelated reply target',
                    'row_address': f'row:{chat}@2026-11-10T00:00:00Z#123456789abc',
                    'ts': '2026-11-10T00:00:00Z'}], 'has_more': False,
                    'window': {'complete': True}, 'coverage': {'complete': True}}); return
            if path.startswith('/api/extensions/project-boards/'):
                kind = path.rsplit('/', 1)[-1]
                if req.method == 'GET':
                    if h['hold'] == kind:
                        h['held'].append(route); return
                    if kind == 'list':
                        project = q['project_id'][0]
                        population = [v for v in items.values() if v['project_id'] == project]
                        route.fulfill(json={'ok': True, 'items': population, 'counts': {'idea': len(population)},
                            'total': len(population), 'offset': 0, 'limit': 100, 'has_more': False}); return
                    if kind == 'item':
                        key = q['item_id'][0]
                        route.fulfill(json={'ok': True, 'item': items[key], 'comments': h['comments'][key], 'needed_by': []}); return
                payload = req.post_data_json; h['writes'].append((kind, copy.deepcopy(payload)))
                if h['hold'] == kind:
                    h['held'].append(route); return
                reply = h['receipts'].get(payload['request_id'])
                if reply is None:
                    key = payload.get('item_id', payload.get('project_id'))
                    if kind == 'update':
                        if payload['expected_revision'] != items[key]['revision']:
                            route.fulfill(status=409, json={'ok': False, 'error': {'code': 'revision_conflict'}, 'current': items[key]}); return
                        items[key].update(payload['changes']); items[key]['revision'] += 1
                    elif kind == 'comment':
                        h['comments'][key].append({'id': 'comment-new', 'text': payload['text'],
                            'created_at': '2026-11-11T00:00:00Z', 'author': {'surface': 'interface'}})
                        items[key]['revision'] += 1
                    elif kind == 'capture':
                        items[key] = {**items[key], 'title': payload['title']}
                    reply = {'ok': True, 'item': copy.deepcopy(items[key])}
                    h['receipts'][payload['request_id']] = reply
                if h['lost'] == kind:
                    h['lost'] = ''; route.abort(); return
                route.fulfill(json=reply); return
            # Real Project catalog/detail/CAS and masked Settings over the temporary data owner.
            if path.startswith('/api/projects') or path == '/api/settings':
                if h['hold'] == path:
                    h['held'].append(route); return
                route.continue_(); return
            if req.method != 'GET':
                # Normal UI read acknowledgements stay inert; no task/chat control is forwarded.
                route.fulfill(json={'ok': True}); return
            route.fulfill(json={})
        context.route('**/*', respond)
        page = context.new_page(); page.on('pageerror', lambda e: h['errors'].append(str(e)))
        page.goto(url + '/?normal=1')
        page.locator('#page-chat #chat-input').wait_for()
        h.update(page=page, url=url, rows=rows)
        try: yield h
        finally:
            evidence = os.environ.get('OUROBOROS_UI_EVIDENCE_DIR')
            if evidence:
                out = Path(evidence); out.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out / ('navigation-' + str(len(list(out.glob('navigation-*.png')))) + '.png')))
            context.close(); browser.close()


def dashboard_board(h):
    page = h['page']
    page.locator('[data-nav-page="dashboard"]').click()
    page.locator('#dashboard-tab-projects').click()
    page.locator('[data-pm-row="alpha"]').click()
    page.locator('.pm-actions').get_by_role('button', name='Work Items', exact=True).click()
    page.locator('[data-wi-item="alpha"]').click()
    page.locator('.wi-comment-form textarea').wait_for()


def test_normal_dashboard_chat_transfer_does_not_reject_its_own_navigation(navigation_ui):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    # This normal open is a surviving positive; shell focus restoration is tested separately.
    page.locator('.wi-detail .wi-actions').first.get_by_role('button', name='Discuss in project chat', exact=True).click()
    page.wait_for_function("document.querySelector('#pchat-alpha-input')?.value.includes('Work Item: alpha')")
    draft = page.locator('#pchat-alpha-input').input_value()
    assert 'Exact selected words α\nSecond line' in draft
    assert 'Project: alpha\nWork Item: alpha' in draft
    assert page.locator('.work-items').is_hidden()
    assert h['frames'] == []
    assert not h['errors'], h['errors']


def test_dashboard_selection_retargets_an_open_board(navigation_ui):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    page.locator('.wi-add input').fill('Alpha capture draft')
    # The modeless board leaves the catalog usable at a wide owner viewport.
    page.locator('[data-pm-row="beta"]').click()
    page.wait_for_function("document.querySelector('[data-wi-project]').textContent === 'Independent project'")
    page.locator('[data-wi-item="beta"]').wait_for()
    assert page.locator('[data-wi-item="beta"]').count() == 1
    assert page.locator('[data-wi-item="alpha"]').count() == 0
    assert page.locator('.wi-comment-form').count() == 0
    page.locator('.wi-add input').fill('Beta capture draft')
    page.locator('[data-pm-row="alpha"]').click()
    page.wait_for_function("document.querySelector('.wi-add input').value === 'Alpha capture draft'")
    page.locator('.wi-comment-form textarea').wait_for()
    assert not h['writes']
    assert not h['errors'], h['errors']


def await_held(h):
    import time
    deadline = time.monotonic() + 10
    while not h['held'] and time.monotonic() < deadline:
        h['page'].wait_for_timeout(10)
    assert h['held'], 'The deliberate request barrier was never reached'


def release_history(h):
    h['hold'] = ''
    for route in h['held']:
        route.fulfill(json={'messages': [], 'has_more': False, 'window': {'complete': True},
                            'coverage': {'complete': True}})
    h['held'].clear()


@pytest.mark.parametrize('interruption', ['shell-focus', 'owner-input', 'owner-navigation', 'close-board'])
def test_transfer_distinguishes_shell_focus_from_new_owner_intent(navigation_ui, interruption):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    h['hold'] = 'history'
    page.get_by_role('button', name='Discuss in project chat', exact=True).click()
    await_held(h)
    page.locator('#pchat-alpha-input').wait_for(state='attached')
    if interruption == 'shell-focus':
        # Supported shell focus restoration is not a user gesture. Before the fix
        # this exact interleaving produced the screenshot's post-open refusal.
        page.locator('[data-nav-page="settings"]').evaluate('n => n.focus()')
    elif interruption == 'owner-input':
        page.locator('.wi-add input').fill('I moved on to capture another thought')
    elif interruption == 'owner-navigation':
        page.locator('[data-nav-page="files"]').click()
    else:
        page.locator('[data-wi-close]').click()
    release_history(h)
    if interruption == 'shell-focus':
        page.wait_for_function("document.querySelector('#pchat-alpha-input')?.value.includes('Work Item: alpha')")
        assert page.locator('.work-items').is_hidden()
    elif interruption == 'owner-input':
        page.locator('.wi-feedback.wi-error').wait_for()
        assert page.locator('#pchat-alpha-input').input_value() == ''
        assert page.locator('.wi-add input').input_value() == 'I moved on to capture another thought'
    else:
        # A late history reply cannot append into a retired or hidden destination.
        page.wait_for_function("!document.querySelector('.wi-list[aria-busy=true]')")
        assert not page.locator('#pchat-alpha-input').count() or page.locator('#pchat-alpha-input').input_value() == ''
    assert h['frames'] == []
    assert not h['errors'], h['errors']


def test_transfer_removes_saved_unrelated_reply_but_keeps_existing_words(navigation_ui):
    h = navigation_ui; page = h['page']
    page.locator('.nav-project-row[data-project-id="alpha"]').click()
    page.locator('#pchat-alpha-messages [data-row-reply]').first.click()
    page.locator('#pchat-alpha-input').fill('Existing room draft')
    chat_id = h['rows'][0]['chat_id']
    assert page.evaluate('key => !!sessionStorage.getItem(key)', 'ouro_chat_reply:' + str(chat_id))
    dashboard_board(h)
    page.get_by_role('button', name='Discuss in project chat', exact=True).click()
    page.wait_for_function("document.querySelector('#pchat-alpha-input')?.value.includes('Work Item: alpha')")
    assert page.locator('#pchat-alpha-input').input_value().startswith('Existing room draft\n')
    assert not page.evaluate('key => !!sessionStorage.getItem(key)', 'ouro_chat_reply:' + str(chat_id))
    assert 'earlier Reply target was removed' in page.locator('#toast-stack').inner_text()
    assert h['frames'] == []


@pytest.mark.parametrize('operation', ['comment', 'update'])
def test_project_follow_preserves_drafts_cas_and_unknown_request(navigation_ui, operation):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    page.locator('.wi-comment-form textarea').fill('Alpha comment draft')
    page.get_by_role('button', name='Edit item', exact=True).click()
    page.locator('.wi-edit input[name=title]').fill('Alpha edit draft')
    original_revision = h['items']['alpha']['revision']
    h['lost'] = operation
    if operation == 'comment':
        page.locator('.wi-comment-form button[type=submit]').click()
        retry_name = 'Retry comment with same request'
    else:
        page.get_by_role('button', name='Save item', exact=True).click()
        retry_name = 'Retry same request'
    page.get_by_role('button', name=retry_name, exact=True).wait_for()
    first = copy.deepcopy(h['writes'][0])
    page.locator('[data-pm-row="beta"]').click()
    page.locator('[data-wi-item="beta"]').wait_for()
    page.locator('[data-wi-item="beta"]').click()
    page.locator('.wi-comment-form textarea').fill('Beta stays separate')
    page.locator('.wi-add input').fill('Beta capture stays separate')
    page.locator('[data-pm-row="alpha"]').click()
    page.get_by_role('button', name=retry_name, exact=True).wait_for()
    assert page.locator('.wi-edit input[name=title]').input_value() == 'Alpha edit draft'
    assert page.locator('.wi-comment-form textarea').input_value() == 'Alpha comment draft'
    page.get_by_role('button', name=retry_name, exact=True).click()
    page.wait_for_function("!document.querySelector('.wi-detail')?.textContent.includes('not confirmed')")
    assert h['writes'] == [first, first]
    if operation == 'update': assert first[1]['expected_revision'] == original_revision
    page.locator('[data-pm-row="beta"]').click()
    page.locator('.wi-comment-form textarea').wait_for()
    assert page.locator('.wi-comment-form textarea').input_value() == 'Beta stays separate'
    assert page.locator('.wi-add input').input_value() == 'Beta capture stays separate'
    assert len(h['comments']['alpha']) == int(operation == 'comment')


def test_follow_switch_clears_old_detail_before_delayed_read_and_stays_closed(navigation_ui):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    page.locator('[data-pm-row="beta"]').click()
    page.locator('[data-wi-item="beta"]').wait_for(); page.locator('[data-wi-item="beta"]').click()
    page.locator('.wi-comment-form textarea').wait_for()
    h['hold'] = 'item'
    page.locator('[data-pm-row="alpha"]').click(); await_held(h)
    assert page.locator('.wi-comment-form').count() == 0
    assert page.get_by_role('button', name='Save item', exact=True).count() == 0
    page.locator('[data-pm-row="beta"]').click()
    # Alpha's response arrives after Beta is selected, even if an endpoint completes after abort.
    route = h['held'].pop(0)
    route.fulfill(json={'ok': True, 'item': h['items']['alpha'], 'comments': [], 'needed_by': []})
    assert page.locator('[data-wi-project]').inner_text() == 'Independent project'
    page.locator('[data-wi-close]').click()
    page.locator('[data-pm-row="alpha"]').click()
    assert page.locator('.work-items').is_hidden()
    assert not h['writes']


@pytest.mark.parametrize('operation', ['capture', 'comment', 'update'])
def test_pending_mutation_settles_original_item_without_retargeting_visible_board(navigation_ui, operation):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    h['hold'] = operation
    if operation == 'capture':
        page.locator('.wi-add input').fill('Pending capture in Alpha')
        page.locator('.wi-add button').click()
    elif operation == 'comment':
        page.locator('.wi-comment-form textarea').fill('Pending Alpha comment')
        page.locator('.wi-comment-form button[type=submit]').click()
    else:
        page.get_by_role('button', name='Edit item', exact=True).click()
        page.locator('.wi-edit input[name=title]').fill('Pending Alpha edit')
        page.get_by_role('button', name='Save item', exact=True).click()
    await_held(h)
    route = h['held'].pop(); payload = copy.deepcopy(route.request.post_data_json)
    page.locator('[data-pm-row="beta"]').click()
    page.locator('[data-wi-item="beta"]').wait_for()
    page.locator('.wi-add input').fill('Beta draft untouched')
    h['hold'] = ''
    if operation == 'update': h['items']['alpha'].update(payload['changes'])
    if operation == 'comment': h['comments']['alpha'].append({'id': 'pending-comment', 'text': payload['text']})
    if operation == 'capture': h['items']['alpha']['title'] = payload['title']
    h['items']['alpha']['revision'] += 1
    route.fulfill(json={'ok': True, 'item': h['items']['alpha']})
    # Wait for the original callback to finish, not a timeout interpreted as success.
    page.locator('[data-pm-row="alpha"]').click()
    if operation == 'capture':
        page.wait_for_function("document.querySelector('.wi-capture-status').textContent.includes('Item saved')")
    elif operation == 'comment':
        page.wait_for_function("document.querySelector('.wi-comment-form textarea')?.value === ''")
    else:
        page.get_by_role('button', name='Edit item', exact=True).wait_for()
    assert h['writes'] == [(operation, payload)]
    assert payload.get('project_id', payload.get('item_id')) == 'alpha'
    page.locator('[data-pm-row="beta"]').click()
    page.wait_for_function("document.querySelector('.wi-add input').value === 'Beta draft untouched'")
    assert page.locator('[data-wi-project]').inner_text() == 'Independent project'


def test_same_project_refresh_keeps_editor_node_selection_and_original_conflict_base(navigation_ui):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    page.get_by_role('button', name='Edit item', exact=True).click()
    field = page.locator('.wi-edit input[name=title]'); field.fill('Keep this edit')
    handle = field.element_handle()
    field.evaluate('n => {n.focus(); n.setSelectionRange(2,7)}')
    # A supported manager refresh restores its own controls; board notification
    # must not repaint the Work Item editor or take the caret.
    page.locator('[data-pm-refresh]').evaluate('n => n.click()')
    page.wait_for_function("!document.querySelector('[data-pm-detail]').hasAttribute('aria-busy')")
    assert field.evaluate('(n, old) => n === old && n === document.activeElement', handle)
    assert field.evaluate('n => [n.selectionStart,n.selectionEnd]') == [2, 7]
    h['items']['alpha']['revision'] = 7
    page.get_by_role('button', name='Save item', exact=True).click()
    page.locator('.wi-conflict').wait_for()
    assert h['writes'][0][1]['expected_revision'] == 1
    assert h['items']['alpha']['title'] == 'Discuss alpha'


def test_selected_excerpt_and_comment_action_keep_source_and_do_not_submit(navigation_ui):
    h = navigation_ui; page = h['page']; dashboard_board(h)
    textarea = page.locator('.wi-comment-form textarea'); textarea.fill('Unsubmitted local comment')
    page.locator('[data-wi-add-comment]').click()
    assert textarea.evaluate('n => n === document.activeElement')
    assert not h['writes'] and not h['frames']
    page.locator('.wi-detail .wi-section .wi-text').first.evaluate('''n => {
        const range = document.createRange(); range.setStart(n.firstChild,0); range.setEnd(n.firstChild,22);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }''')
    selected = page.evaluate('getSelection().toString()')
    page.get_by_role('button', name='Discuss in project chat', exact=True).focus()
    page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('#pchat-alpha-input')?.value.includes('Work Item: alpha')")
    draft = page.locator('#pchat-alpha-input').input_value()
    assert draft.startswith(selected + '\n\nProject: alpha\nWork Item: alpha')
    assert 'Second line' not in draft
    assert not h['writes'] and not h['frames']


@pytest.mark.parametrize('other_project', [False, True])
def test_linked_item_switch_retires_old_actionable_editor_during_read(navigation_ui, other_project):
    h = navigation_ui; page = h['page']
    home = 'beta' if other_project else 'alpha'
    h['items']['beta']['project_id'] = home
    h['items']['alpha']['prerequisites'] = [{'item_id': 'beta', 'project_id': home,
                                            'title': 'Linked item', 'disposition': 'idea'}]
    dashboard_board(h)
    page.get_by_role('button', name='Edit item', exact=True).click()
    page.locator('.wi-edit input[name=title]').fill('Keep Alpha draft and CAS')
    h['hold'] = 'item'
    page.get_by_role('button', name='Linked item · ' + home + ' · idea', exact=True).click()
    await_held(h)
    assert page.locator('.wi-edit').count() == 0
    assert page.locator('.wi-comment-form').count() == 0
    h['hold'] = ''
    h['held'].pop().fulfill(json={'ok': True, 'item': h['items']['beta'], 'comments': [], 'needed_by': []})
    page.locator('.wi-detail h3').filter(has_text='Discuss beta').wait_for()
    assert not h['writes']
    # Alpha's unsaved editor and base survive when deliberately returning.
    page.locator('[data-wi-close]').click()
    dashboard_board(h)
    assert page.locator('.wi-edit input[name=title]').input_value() == 'Keep Alpha draft and CAS'
