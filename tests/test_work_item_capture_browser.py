"""Sidebar capture/menu module contracts, not a full SPA or real-board test.

Real WorkItems, mutation, focus, menu and CSS from ui_candidate_checkout; strict
in-memory server replies only. All HTTP is fulfilled/aborted and sockets closed.
The existing full-app consumer owns Dashboard selection/drawer navigation proof.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import tests
from tests.candidate_checkout import case_checkout
from tests.test_subscription_setup_browser import subscription_ui  # noqa: F401

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
REPO = Path(tests.__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures' / 'work_item_capture.html'
DIALOG = '.wi-quick-dialog'
TITLE = DIALOG + ' input[name=title]'
DESCRIPTION = DIALOG + ' textarea[name=description]'
SAVE = '[data-wic-save]'
STATUS = '.wi-quick-status'


@pytest.fixture
def capture_ui(subscription_ui, ui_candidate_checkout, tmp_path):
    """Reuse normal engine/executable and source-copy custody; no app/backend boot."""
    ui = subscription_ui
    page = ui['page']
    with case_checkout(REPO, ui_candidate_checkout) as candidate:
        page.route_web_socket('**/*', lambda ws: ws.close())

        violations = []

        def serve(route):
            u = urlparse(route.request.url)
            if route.request.method == 'GET' and u.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=FIXTURE.read_text(encoding='utf-8'))
                return
            if route.request.method == 'GET' and u.path.startswith('/static/'):
                relative = Path(unquote(u.path.removeprefix('/static/')))
                asset = candidate.path / 'web' / relative
                if not relative.is_absolute() and '..' not in relative.parts and asset.is_file():
                    route.fulfill(path=str(asset))
                    return
            violations.append((route.request.method, route.request.url))
            route.abort()  # assert outside Playwright's callback, never leak to a real endpoint

        page.route('**/*', serve)
        try:
            page.goto(ui['url'] + '/', wait_until='domcontentloaded')
            page.wait_for_function('window.ready === true')
            yield page
            page.screenshot(path=str(tmp_path / 'capture-final.png'))
        finally:
            page.close()
        assert violations == [], violations
        assert ui['posts'] == []
        assert ui['reads'] == []


def settle(page):
    page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


def open_capture(page, project='b', name='Project B'):
    page.locator('#origin').focus()
    page.evaluate('([id, name]) => h.open(id, name)', [project, name])
    page.locator(DIALOG).wait_for()


def writes(page):
    return page.evaluate('h.writes.map(w => w.payload)')


def close_capture(page):
    page.keyboard.press('Escape')
    page.locator(DIALOG).wait_for(state='detached')


def assert_focus_owned(page):
    assert page.evaluate("""() => {
        const d = document.querySelector('.wi-quick-dialog'), a = document.activeElement;
        return !!d?.contains(a) && !a.matches(':disabled') && !a.closest('[hidden], [inert]');
    }""")


def assert_origin(page):
    assert page.locator('#origin').evaluate('(n) => n === document.activeElement')


@pytest.mark.parametrize('add,go', [(False, False), (True, False), (False, True), (True, True)])
def test_menu_optional_callbacks_separator_order(capture_ui, add, go):
    page = capture_ui
    page.locator('#origin').focus()
    page.evaluate('([a,g]) => h.menu(a,g)', [add, go])
    menu = page.locator('.project-row-menu:not([data-leaving])')
    assert menu.get_attribute('aria-label') == 'Actions for Project B'
    assert menu.locator('[role=separator]').count() == int(add or go)
    expected = ['pin', 'rename', 'delete'] + (['add-work-item'] if add else []) + (['work-items'] if go else [])
    assert menu.locator('[role=menuitem]').evaluate_all('(nodes) => nodes.map(n => n.dataset.prm)') == expected
    if add or go:
        assert menu.evaluate("""n => {
            const sep = n.querySelector('[role=separator]');
            return sep.previousElementSibling.dataset.prm === 'delete' &&
                ['add-work-item','work-items'].includes(sep.nextElementSibling.dataset.prm);
        }""")
    page.keyboard.press('Escape')
    menu.wait_for(state='detached')
    assert_origin(page)


@pytest.mark.parametrize('action', ['add-work-item', 'work-items'])
def test_real_right_click_keeps_opening_target_not_active_room(capture_ui, action):
    page = capture_ui
    page.locator('#origin').click(button='right')
    menu = page.locator('.project-row-menu:not([data-leaving])')
    page.evaluate("h.target = {id:'c', name:'Changed sidebar selection'}")
    menu.locator('[data-prm="' + action + '"]').click()
    assert page.evaluate('h.actions') == [['add' if action == 'add-work-item' else 'go', 'b']]
    assert page.locator('#chat').input_value() == 'Active room A draft'
    if action == 'add-work-item':
        assert page.locator('#wic-target').inner_text() == 'Project: Project B'
        assert page.locator('.work-items').is_hidden()
    else:
        assert page.locator('[data-wi-project]').inner_text() == 'Project B'
        assert page.locator('.work-items').is_visible()
        assert not page.locator('.work-items').evaluate("n => n.classList.contains('wi-has-detail')")
    assert writes(page) == []


def test_blank_literal_description_ime_singleton_and_passive_save(capture_ui):
    page = capture_ui
    open_capture(page)
    assert page.locator(TITLE).evaluate('(n) => n === document.activeElement')
    page.locator(TITLE).fill('   ')
    page.locator(SAVE).click()
    assert 'title' in page.locator(STATUS).inner_text()
    assert writes(page) == []
    page.locator(TITLE).fill('  Literal title α  ')
    page.evaluate("window.originalDialog = document.querySelector('.wi-quick-dialog'); h.open('b','Renamed')")
    assert page.locator(DIALOG).evaluate('(n) => n === window.originalDialog')
    assert page.locator('#wic-target').inner_text() == 'Project: Project B'
    assert page.locator(TITLE).evaluate("""n => [
        {key:'Enter',isComposing:true}, {key:'Enter',repeat:true}, {key:'Enter',keyCode:229}
    ].map(init => {const e = new KeyboardEvent('keydown',{bubbles:true,cancelable:true,...init});
        n.dispatchEvent(e); return e.defaultPrevented;})""") == [True, True, True]
    assert writes(page) == []
    page.locator(SAVE).focus()
    page.keyboard.press('Tab')
    assert page.locator('[data-wic-cancel=header]').evaluate('(n) => n === document.activeElement')
    page.locator(DESCRIPTION).fill('Line one\nLine two  ')
    page.locator(DESCRIPTION).press('End')
    page.locator(DESCRIPTION).press('Enter')
    assert writes(page) == []
    page.locator(SAVE).click()
    page.locator(DIALOG).wait_for(state='detached')
    payload, = writes(page)
    assert set(payload) == {'project_id', 'title', 'description', 'request_id'}
    assert {k: payload[k] for k in ('project_id','title','description')} == {
        'project_id':'b', 'title':'  Literal title α  ', 'description':'Line one\nLine two  \n'}
    assert payload['request_id']
    assert page.evaluate('h.items[0].disposition') == 'idea'  # supplied by fake server, never sent
    assert page.evaluate('h.writes[0].headers') == {'Content-Type':'application/json'}
    assert page.locator('#chat').input_value() == 'Active room A draft'
    assert_origin(page)


def test_cancel_discards_unsent_but_pending_survives_cross_target(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Discard this')
    close_capture(page)
    open_capture(page)
    assert page.locator(TITLE).input_value() == ''
    page.locator(TITLE).fill('Pending B')
    page.evaluate("h.mode='delay'")
    page.locator(SAVE).click()
    page.wait_for_function('!!h.release')
    before = writes(page)
    page.evaluate("document.querySelector('.wi-quick-dialog form').requestSubmit()")
    assert writes(page) == before
    close_capture(page)
    open_capture(page)
    assert page.locator(TITLE).is_disabled()
    assert page.locator(TITLE).input_value() == 'Pending B'
    assert page.locator(DESCRIPTION).is_disabled()
    assert page.locator(SAVE).is_disabled()
    assert page.locator(SAVE).inner_text() == 'Saving…'
    assert page.locator('[data-wic-cancel=footer]').inner_text() == 'Close'
    assert page.locator('[data-wic-cancel=header]').evaluate('(n) => n === document.activeElement')
    page.evaluate("h.open('c', 'Project C')")
    page.locator(TITLE).fill('C survives B')
    page.evaluate('h.release()')
    page.wait_for_function('h.toasts.length === 1')
    assert page.locator('#wic-target').inner_text() == 'Project: Project C'
    assert page.locator(TITLE).input_value() == 'C survives B'
    assert writes(page) == before
    assert page.evaluate('h.toasts[0].project.id') == 'b'
    close_capture(page)
    open_capture(page)
    assert page.locator(TITLE).input_value() == ''  # B's confirmed draft was retired


def test_unknown_read_never_confirms_and_explicit_retry_reuses_exact_key(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Lost reply')
    page.locator(DESCRIPTION).fill('Exact text  ')
    page.evaluate("h.mode='lost'")
    page.locator(SAVE).click()
    page.locator('[data-wic-retry]').wait_for()
    lost, = writes(page)
    close_capture(page)
    open_capture(page)
    assert writes(page) == [lost]  # no automatic replay on open
    page.locator('[data-wic-check]').click()
    page.wait_for_function("document.querySelector('.wi-quick-status').textContent.includes('Matching text')")
    checked, = page.evaluate('h.reads')
    assert urlparse(checked).path == '/api/extensions/project-boards/list'
    assert parse_qs(urlparse(checked).query)['project_id'] == ['b']
    assert page.locator('[data-wic-check]').is_enabled()
    assert writes(page) == [lost]
    assert page.locator(TITLE).is_disabled()
    page.locator('[data-wic-retry]').click()
    page.locator(DIALOG).wait_for(state='detached')
    assert writes(page) == [lost, lost]
    assert page.evaluate('h.items.length') == 1


def test_typed_refusal_keeps_editable_draft_and_new_attempt_key(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Rejected draft')
    page.locator(DESCRIPTION).fill('Retain this')
    page.evaluate("h.mode='reject'")
    page.locator(SAVE).click()
    page.wait_for_function("document.querySelector('.wi-quick-status').textContent === 'Readonly board'")
    first, = writes(page)
    assert page.locator(TITLE).is_enabled()
    assert page.locator(TITLE).input_value() == first['title']
    assert page.locator(DESCRIPTION).input_value() == first['description']
    page.locator(TITLE).fill('Corrected draft')
    page.locator(TITLE).press('Enter')
    page.locator(DIALOG).wait_for(state='detached')
    assert writes(page)[1]['request_id'] != first['request_id']


@pytest.mark.parametrize('mode', ['mismatch-project', 'mismatch-title', 'mismatch-description',
                                 'mismatch-state', 'bad-json', 'null-data'])
def test_incomplete_or_wrong_target_receipt_stays_unknown(capture_ui, mode):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Not confirmed')
    page.evaluate('mode => h.mode=mode', mode)
    page.locator(SAVE).click()
    page.locator('[data-wic-retry]').wait_for()
    assert page.locator(TITLE).is_disabled()
    assert page.locator(TITLE).input_value() == 'Not confirmed'
    assert page.evaluate('h.toasts.length') == 0
    assert 'not confirmed' in page.locator(STATUS).inner_text()


def test_quick_capture_refreshes_open_list_without_navigation(capture_ui):
    page = capture_ui
    page.evaluate("h.wi.open({id:'b',name:'Project B'})")
    page.wait_for_function("document.querySelector('.wi-list-status').textContent === ''")
    page.locator('.wi-add input').fill('Existing list draft')
    open_capture(page)
    page.locator(TITLE).fill('Refresh the open list')
    page.locator(SAVE).click()
    page.locator(DIALOG).wait_for(state='detached')
    page.wait_for_function("[...document.querySelectorAll('.wi-row strong')].some(n => n.textContent === 'Refresh the open list')")
    assert page.locator('.work-items').is_visible()
    assert page.locator('.wi-add input').input_value() == 'Existing list draft'
    assert page.locator('#chat').input_value() == 'Active room A draft'


def test_destroy_aborts_pending_and_cannot_reopen(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Destroyed pending')
    page.evaluate("h.mode='delay'")
    page.locator(SAVE).click()
    page.wait_for_function('!!h.release')
    page.evaluate('h.destroy(); h.destroy()')  # teardown is idempotent
    settle(page)
    assert page.locator(DIALOG).count() == page.locator('.work-items').count() == 0
    assert page.evaluate('h.aborted') == 1
    assert page.evaluate('h.toasts.length') == 0
    page.evaluate('h.release(); h.open()')
    settle(page)
    assert page.evaluate('h.toasts.length') == 0
    page.evaluate('h.open()')
    assert page.locator(DIALOG).count() == 0


@pytest.mark.parametrize('mode,submit', [('delay','enter'), ('delay','save'), ('lost','enter'), ('lost','save')])
def test_disabled_focused_submit_escape_and_tab_remain_modal(capture_ui, mode, submit):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill(mode + submit)
    page.evaluate('mode => h.mode=mode', mode)
    page.locator(TITLE).press('Enter') if submit == 'enter' else page.locator(SAVE).click()
    page.wait_for_function('!!h.release') if mode == 'delay' else page.locator('[data-wic-retry]').wait_for()
    settle(page)
    assert_focus_owned(page)
    close_capture(page)  # directly BEFORE Tab could accidentally recover escaped focus
    assert_origin(page)
    open_capture(page)
    if mode == 'lost':
        page.evaluate("h.mode='delay'; h.release=null")
        page.locator('[data-wic-retry]').click()
        page.wait_for_function('!!h.release')
        settle(page)
        assert_focus_owned(page)
    for _ in range(10):
        page.keyboard.press('Tab')
        assert_focus_owned(page)
    page.keyboard.press('Shift+Tab')
    assert_focus_owned(page)
    close_capture(page)
    assert_origin(page)
    page.evaluate('h.release()')
    page.wait_for_function('h.toasts.length === 1')
    assert_origin(page)


def test_disabled_focused_read_check_escape_and_late_reply(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Read check')
    page.evaluate("h.mode='lost'")
    page.locator(SAVE).click()
    page.locator('[data-wic-check]').wait_for()
    page.evaluate('h.readDelay=true')
    page.locator('[data-wic-check]').click()
    page.wait_for_function('!!h.checkRelease')
    settle(page)
    assert_focus_owned(page)
    for _ in range(10):
        page.keyboard.press('Tab')
        assert_focus_owned(page)
    close_capture(page)
    assert_origin(page)
    page.evaluate('h.checkRelease()')
    settle(page)
    assert_origin(page)
    assert len(writes(page)) == 1


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_long_target_narrow_capture_fits(capture_ui, theme):
    page = capture_ui
    page.set_viewport_size({'width':390,'height':740})
    page.emulate_media(color_scheme=theme)
    open_capture(page, 'long-target', 'A very long project name ' * 12)
    # This module fixture has no theme.js: media alone does not select ui.css.
    other = 'light' if theme == 'dark' else 'dark'
    page.evaluate('t => document.documentElement.dataset.theme = t', other)
    other_ink = page.locator(DIALOG).evaluate('n => getComputedStyle(n).backgroundColor')
    page.evaluate('t => document.documentElement.dataset.theme = t', theme)
    assert page.locator(DIALOG).evaluate('n => getComputedStyle(n).backgroundColor') != other_ink
    page.locator(TITLE).fill('Capture a small idea')
    page.locator(DESCRIPTION).fill('No workflow or running agent is changed.')
    settle(page)
    facts = page.locator(DIALOG).evaluate("""n => {
        const r = n.getBoundingClientRect();
        return {left:r.left,right:r.right,window:innerWidth,overflow:n.scrollWidth > n.clientWidth};
    }""")
    assert facts['left'] >= 0 and facts['right'] <= facts['window'] and not facts['overflow'], json.dumps(facts)


@pytest.mark.parametrize('control', ['header', 'footer', 'backdrop'])
def test_cancel_controls_discard_only_unsent_draft(capture_ui, control):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Unsent draft')
    page.locator(DESCRIPTION).click()  # inside clicks do not dismiss
    assert page.locator(DIALOG).count() == 1
    assert page.locator('[data-wic-cancel=footer]').inner_text() == 'Cancel'
    if control == 'backdrop':
        page.locator('.wi-quick-backdrop').click(position={'x':2, 'y':2})
    else:
        page.locator('[data-wic-cancel=' + control + ']').click()
    page.locator(DIALOG).wait_for(state='detached')
    open_capture(page)
    assert page.locator(TITLE).input_value() == ''
    assert writes(page) == []


def test_unknown_draft_lives_only_for_current_page(capture_ui):
    page = capture_ui
    open_capture(page)
    page.locator(TITLE).fill('Unknown on old page')
    page.evaluate("h.mode='lost'")
    page.locator(SAVE).click()
    page.locator('[data-wic-retry]').wait_for()
    old, = writes(page)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_function('window.ready === true')
    open_capture(page)
    assert page.locator(TITLE).input_value() == ''
    assert writes(page) == []  # reload did not replay the old unresolved operation
    page.locator(TITLE).fill('New deliberate capture')
    page.locator(SAVE).click()
    page.locator(DIALOG).wait_for(state='detached')
    new, = writes(page)
    assert new['request_id'] != old['request_id']


def seed_board(page):
    page.evaluate("""() => {
        const item = (id, disposition, title, archived = false, project_id = 'b') => ({
            id, project_id, title, disposition, archived, revision: 1,
            description: 'Preserved description\\n'.repeat(30), outcome: '',
            references: [], prerequisites: [], comment_count: 0,
            created_at: '2026-11-10T09:00:00Z', updated_at: '2026-11-10T09:00:00Z',
            created_by: {surface: 'interface'}
        });
        h.items = [
            ...Array.from({length: 102}, (_, i) => item('p-' + i, 'planned', 'Needle planned ' + i)),
            item('i-1', 'idea', 'Needle idea'), item('i-2', 'idea', 'Unmatched idea'),
            item('d-1', 'done', 'Needle done'),
            item('a-1', 'set_aside', 'Needle archived', true),
            item('c-1', 'idea', 'Other room', false, 'c')
        ];
        h.comments.set('p-0', [{id: 'original-comment', text: 'Quoted original',
            created_at: '2026-11-10T09:00:00Z', created_by: {surface: 'agent'}}]);
    }""")


def open_board(page, project='b', name='Project B', item=''):
    page.evaluate("([id,name,item]) => h.wi.open({id,name}, item)", [project, name, item])
    wait_board_list(page)


def wait_board_list(page):
    page.wait_for_function("""() => {
        const root = document.querySelector('.work-items');
        return !root.hidden && !root.querySelector('.wi-list').hasAttribute('aria-busy') &&
            root.querySelector('.wi-list-status').textContent === '';
    }""")


def choose_category(page, key):
    page.locator('[data-wi-filter="' + key + '"]').click()
    wait_board_list(page)


def category_counts(page):
    return page.locator('[data-wi-count]').evaluate_all(
        'nodes => Object.fromEntries(nodes.map(n => [n.dataset.wiCount, n.textContent]))')


def test_persistent_categories_search_archive_pages_and_room_state(capture_ui):
    page = capture_ui
    seed_board(page)
    open_board(page)
    assert page.locator('.wi-search select').count() == 0
    assert page.locator('.wi-list details').count() == 0
    assert page.locator('[data-wi-count]').count() == 4
    assert category_counts(page) == {'planned': '102', 'idea': '2', 'done': '1', 'set_aside': '0'}
    assert page.locator('[data-wi-filter=""]').get_attribute('aria-pressed') == 'true'

    choose_category(page, 'planned')
    assert page.locator('.wi-row').count() == 100
    assert page.locator('[data-wi-page]').inner_text() == '1–100 of 102'
    # Updating counts/results must not replace the activating control.
    assert page.locator('[data-wi-filter="planned"]').evaluate('n => n === document.activeElement')
    page.locator('[data-wi-next]').click()
    wait_board_list(page)
    assert page.locator('.wi-row').count() == 2
    assert page.locator('[data-wi-page]').inner_text() == '101–102 of 102'
    assert category_counts(page)['planned'] == '102'  # not this page's two rows

    page.locator('.wi-search input').fill('Needle')
    page.locator('.wi-search button').click()
    wait_board_list(page)
    assert page.locator('[data-wi-page]').inner_text() == '1–100 of 102'
    assert category_counts(page) == {'planned': '102', 'idea': '1', 'done': '1', 'set_aside': '0'}
    assert page.locator('[data-wi-filter="planned"]').get_attribute('aria-pressed') == 'true'

    choose_category(page, 'idea')
    assert page.locator('.wi-row').count() == 1
    assert category_counts(page)['planned'] == '102'  # before category filtering
    page.locator('[data-wi-archived]').check()
    wait_board_list(page)
    assert page.locator('.wi-row').count() == 0
    assert category_counts(page) == {'planned': '0', 'idea': '0', 'done': '0', 'set_aside': '1'}
    choose_category(page, 'set_aside')
    assert page.locator('.wi-row').count() == 1

    open_board(page, 'c', 'Project C')
    assert page.locator('.wi-search input').input_value() == ''
    assert not page.locator('[data-wi-archived]').is_checked()
    assert page.locator('[data-wi-filter=""]').get_attribute('aria-pressed') == 'true'
    open_board(page)
    assert page.locator('.wi-search input').input_value() == 'Needle'
    assert page.locator('[data-wi-archived]').is_checked()
    assert page.locator('[data-wi-filter="set_aside"]').get_attribute('aria-pressed') == 'true'
    choose_category(page, '')
    assert page.locator('.wi-row').count() == 1
    assert writes(page) == []
    assert page.evaluate('h.transfers') == []


def test_add_comment_focuses_existing_editor_without_repaint_or_submission(capture_ui):
    page = capture_ui
    seed_board(page)
    open_board(page, item='p-0')
    page.locator('.wi-comment-form textarea').wait_for()
    textarea = page.locator('.wi-comment-form textarea')
    textarea.fill('Existing comment draft  ')
    page.locator('.wi-comment').get_by_role('button', name='Quote', exact=True).click()
    textarea = page.locator('.wi-comment-form textarea')
    page.evaluate("""() => {
        window.keptCommentForm = document.querySelector('.wi-comment-form');
        window.keptCommentInput = keptCommentForm.querySelector('textarea');
        keptCommentInput.setSelectionRange(3, 8);
        document.querySelector('.wi-detail').scrollTop = 0;
    }""")
    page.locator('[data-wi-add-comment]').click()
    assert textarea.evaluate('n => n === window.keptCommentInput && n === document.activeElement')
    assert page.locator('.wi-comment-form').evaluate('n => n === window.keptCommentForm')
    assert textarea.input_value() == 'Existing comment draft  '
    assert textarea.evaluate('n => [n.selectionStart, n.selectionEnd]') == [3, 8]
    assert 'Quoted original' in page.locator('.wi-comment-form blockquote').inner_text()
    assert textarea.evaluate("""n => {
        const r = n.getBoundingClientRect(), h = n.closest('.wi-detail').getBoundingClientRect();
        return r.top >= h.top && r.bottom <= h.bottom;
    }"""), textarea.evaluate("n => ({input:n.getBoundingClientRect().toJSON(),host:n.closest('.wi-detail').getBoundingClientRect().toJSON()})")
    assert writes(page) == []
    assert page.evaluate('h.transfers') == []
    assert page.locator('.wi-detail').get_by_role('button', name='Discuss in project chat', exact=True).count() == 1


@pytest.mark.parametrize('mode', ['delay', 'lost'])
def test_comment_shortcut_respects_existing_pending_and_unknown_request(capture_ui, mode):
    page = capture_ui
    seed_board(page)
    open_board(page, item='p-0')
    textarea = page.locator('.wi-comment-form textarea')
    textarea.wait_for()
    textarea.fill('Kept during request')
    page.evaluate('mode => h.commentMode = mode', mode)
    page.locator('.wi-comment-form button[type=submit]').click()
    if mode == 'delay':
        page.wait_for_function('!!h.commentRelease')
    else:
        page.get_by_role('button', name='Retry comment with same request', exact=True).wait_for()
    first, = writes(page)
    assert textarea.is_disabled()
    assert page.locator('[data-wi-add-comment]').is_disabled()
    assert textarea.input_value() == 'Kept during request'
    if mode == 'delay':
        page.evaluate('h.commentRelease()')
    else:
        page.get_by_role('button', name='Retry comment with same request', exact=True).click()
    page.wait_for_function("""() => {
        const n = document.querySelector('.wi-comment-form textarea');
        return n && !n.disabled && n.value === '';
    }""")
    assert page.locator('[data-wi-add-comment]').is_enabled()
    assert writes(page) == ([first] if mode == 'delay' else [first, first])
    assert page.evaluate("h.comments.get('p-0').length") == 2  # one original + one new, even after retry


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('width', [390, 1440])
def test_category_controls_fit_both_palettes_and_narrow_list(capture_ui, theme, width):
    page = capture_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""({theme,width}) => {
        document.documentElement.dataset.theme = theme;
        document.documentElement.dataset.layout = width < 760 ? 'stack' : 'wide';
    }""", {'theme': theme, 'width': width})
    seed_board(page)
    open_board(page)
    choose_category(page, 'set_aside')  # zero remains visible, selectable and empty
    assert page.locator('[data-wi-filter="set_aside"]').get_attribute('aria-pressed') == 'true'
    assert page.locator('.wi-row').count() == 0
    settle(page)
    assert page.locator('.wi-categories').evaluate("""n => {
        const buttons = [...n.querySelectorAll('button')], r = n.getBoundingClientRect();
        return n.scrollWidth <= n.clientWidth && buttons.every(b => {
            const x = b.getBoundingClientRect();
            return x.width > 0 && x.height > 0 && x.left >= r.left && x.right <= r.right &&
                b.scrollWidth <= b.clientWidth;
        });
    }""")
    assert page.locator('.work-items').evaluate("""n => {
        const r = n.getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth && n.scrollWidth <= n.clientWidth;
    }""")
    assert writes(page) == []


def open_move(page):
    section = page.locator('.wi-move')
    if not section.evaluate('n => n.open'):
        section.locator('summary').click()


def test_move_cas_preserves_unsaved_edit_base_and_comment_draft(capture_ui):
    page = capture_ui
    seed_board(page)
    open_board(page, item='p-0')
    page.get_by_role('button', name='Edit item', exact=True).click()
    page.locator('.wi-edit input[name=title]').fill('Unsent title')
    page.locator('.wi-comment-form textarea').fill('Unsent comment')
    page.evaluate("() => {h.items[0].revision++; h.items[0].outcome='Remote outcome';}")
    open_move(page)
    page.get_by_role('button', name='Project Q', exact=True).click()
    page.locator('.wi-move').get_by_text('Item changed', exact=False).wait_for()
    page.get_by_role('button', name='Project Q', exact=True).click()
    page.wait_for_function("document.querySelector('[data-wi-project]').textContent === 'Project Q'")
    assert page.locator('.wi-edit input[name=title]').input_value() == 'Unsent title'
    assert page.locator('.wi-comment-form textarea').input_value() == 'Unsent comment'
    assert page.evaluate('h.items[0].project_id') == 'q'
    page.get_by_role('button', name='Save item', exact=True).click()
    page.locator('.wi-conflict').wait_for(state='visible')
    assert page.evaluate('h.items[0].outcome') == 'Remote outcome'
    assert page.evaluate('h.items[0].title') != 'Unsent title'


@pytest.mark.parametrize('mode', ['lost', 'refresh-fail'])
def test_move_keeps_unknown_receipt_and_distinguishes_saved_refresh_failure(capture_ui, mode):
    page = capture_ui
    seed_board(page)
    open_board(page, item='p-0')
    page.evaluate('mode => h.updateMode=mode', mode)
    open_move(page)
    page.get_by_role('button', name='Project Q', exact=True).click()
    if mode == 'lost':
        page.get_by_role('button', name='Retry same move', exact=True).wait_for()
        original, = writes(page)
        page.get_by_role('button', name='Retry same move', exact=True).click()
        page.wait_for_function("document.querySelector('[data-wi-project]').textContent === 'Project Q'")
        assert writes(page) == [original, original]
    else:
        page.locator('.wi-move').get_by_text('Move saved. Current location could not be refreshed', exact=False).wait_for()
        page.get_by_role('button', name='Refresh current item', exact=True).click()
        page.get_by_role('button', name='Open in current project', exact=True).click()
        page.wait_for_function("document.querySelector('[data-wi-project]').textContent === 'Project Q'")
    assert page.evaluate('h.items[0].id') == 'p-0'
    assert page.evaluate('h.items[0].revision') == 2
