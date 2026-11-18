"""Explicit known references through normal SPA history/Markdown and existing board owners.

Board/history/socket responses are controlled; Project routes use real disposable data.
No live provider, owner mutation or inference. Separate payload consumers cover storage.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from tests.test_work_item_navigation_browser import (  # noqa: F401
    navigation_ui, model_server, dashboard_board, await_held,
)

pytestmark = pytest.mark.ui_browser
A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
MISSING = '33333333-3333-4333-8333-333333333333'


def seed(h):
    for key, project in [(A, 'alpha'), (B, 'beta')]:
        h['items'][key] = {**copy.deepcopy(h['items'][project]), 'id': key,
            'title': 'Exact ' + project + ' outcome', 'outcome': 'Delivered without an automatic send'}
        h['comments'][key] = [{'id': 'comment-' + key, 'text': 'Existing attributed discussion'}]
    h['history_text'] = f'[Alpha work](/?work_item={A}) and [Beta work](/?work_item={B}). [Unavailable work](/?work_item={MISSING})'
    h['page'].reload()
    h['page'].get_by_role('link', name='Alpha work', exact=True).wait_for()


def extension_reads(h, kind='item'):
    return [url for url in h['reads'] if urlparse(url).path == '/api/extensions/project-boards/' + kind]


def test_normal_chat_link_is_inert_until_click_and_preserves_chat_and_board_drafts(navigation_ui):
    h = navigation_ui; page = h['page']; seed(h)
    assert not extension_reads(h) and not extension_reads(h, 'list')
    page.locator('#chat-input').fill('Main draft remains unsent')
    page.get_by_role('link', name='Alpha work', exact=True).click()
    page.locator('.wi-detail h3').filter(has_text='Exact alpha outcome').wait_for()
    assert 'Existing attributed discussion' in page.locator('.wi-detail').inner_text()
    assert 'Delivered without an automatic send' in page.locator('.wi-detail').inner_text()
    page.locator('.wi-add input').fill('Alpha capture draft')
    page.locator('.wi-comment-form textarea').fill('Unsubmitted Alpha comment')
    page.get_by_role('button', name='Edit item', exact=True).click()
    page.locator('.wi-edit input[name=title]').fill('Alpha edit keeps its CAS base')
    page.locator('[data-wi-close]').click()
    page.get_by_role('link', name='Beta work', exact=True).click()
    page.locator('.wi-detail h3').filter(has_text='Exact beta outcome').wait_for()
    page.locator('[data-wi-close]').click()
    page.get_by_role('link', name='Alpha work', exact=True).click()
    page.locator('.wi-edit input[name=title]').wait_for()
    assert page.locator('.wi-edit input[name=title]').input_value() == 'Alpha edit keeps its CAS base'
    assert page.locator('.wi-comment-form textarea').input_value() == 'Unsubmitted Alpha comment'
    assert page.locator('.wi-add input').input_value() == 'Alpha capture draft'
    assert page.locator('#chat-input').input_value() == 'Main draft remains unsent'
    assert [parse_qs(urlparse(u).query)['item_id'][0] for u in extension_reads(h)] == [A, B, A]
    assert not h['writes'] and not h['frames'] and not h['errors']


def test_explicit_startup_copy_link_keyboard_and_current_moved_archived_home(navigation_ui):
    h = navigation_ui; page = h['page']; seed(h)
    h['items'][A].update(project_id='beta', archived=True)
    page.context.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedLink=text}}})")
    page.goto(h['url'] + '/?work_item=' + A)
    page.locator('.wi-detail h3').filter(has_text='Exact alpha outcome').wait_for()
    assert page.locator('[data-wi-project]').inner_text() == 'beta'
    assert page.locator('.wi-item-identity').inner_text().startswith('Item ID: ' + A)
    page.get_by_role('button', name='Copy link', exact=True).click()
    page.wait_for_function('!!window.copiedLink')
    assert page.evaluate('window.copiedLink') == h['url'] + '/?work_item=' + A
    page.locator('[data-wi-close]').click()
    link = page.get_by_role('link', name='Beta work', exact=True)
    link.focus(); page.keyboard.press('Enter')
    page.locator('.wi-detail h3').filter(has_text='Exact beta outcome').wait_for()
    assert page.locator('.wi-detail').evaluate('n=>n===document.activeElement')
    assert not h['writes'] and not h['frames'] and not h['errors']


@pytest.mark.parametrize('later', ['another-link', 'navigation'])
def test_late_exact_read_cannot_replace_newer_intent(navigation_ui, later):
    h = navigation_ui; page = h['page']; seed(h)
    h['hold'] = 'item'
    page.get_by_role('link', name='Alpha work', exact=True).click(); await_held(h)
    old = h['held'].pop()
    if later == 'another-link':
        page.get_by_role('link', name='Beta work', exact=True).click(); await_held(h)
        current = h['held'].pop(); h['hold'] = ''
        current.fulfill(json={'ok': True, 'item': h['items'][B], 'comments': h['comments'][B], 'needed_by': []})
        page.locator('.wi-detail h3').filter(has_text='Exact beta outcome').wait_for()
    else:
        page.locator('[data-nav-page="files"]').click(); h['hold'] = ''
    old.fulfill(json={'ok': True, 'item': h['items'][A], 'comments': h['comments'][A], 'needed_by': []})
    page.wait_for_function("!document.querySelector('a[aria-busy=true]')")
    if later == 'navigation': assert page.locator('.work-items').is_hidden()
    else: assert page.locator('.wi-detail h3').inner_text() == 'Exact beta outcome'
    assert not h['writes'] and not h['errors']


def test_missing_item_is_truthful_and_leaves_existing_draft_and_view(navigation_ui):
    h = navigation_ui; page = h['page']; seed(h)
    page.locator('#chat-input').fill('Do not lose this')
    page.get_by_role('link', name='Unavailable work', exact=True).click()
    page.locator('#toast-stack').filter(has_text='Work Item unavailable').wait_for()
    assert page.locator('.work-items').is_hidden()
    assert page.locator('#chat-input').input_value() == 'Do not lose this'
    assert len(extension_reads(h)) == 1 and not h['writes'] and not h['frames']


def test_closed_project_remains_a_readable_notebook_not_a_chat_send(navigation_ui, model_server):
    from ouroboros import projects_registry as projects
    h = navigation_ui; page = h['page']; seed(h)
    _, data = model_server
    projects.begin_project_deletion(data, 'beta'); projects.complete_project_deletion(data, 'beta')
    page.get_by_role('link', name='Beta work', exact=True).click()
    page.locator('.wi-detail h3').filter(has_text='Exact beta outcome').wait_for()
    page.get_by_role('button', name='Discuss in project chat', exact=True).click()
    page.locator('.wi-error').filter(has_text='closed').wait_for()
    assert not h['frames'] and not h['writes']


@pytest.mark.parametrize('operation', ['comment', 'update'])
@pytest.mark.parametrize('delivery', ['pending', 'unknown'])
def test_link_navigation_keeps_original_mutation_destination_and_identity(navigation_ui, operation, delivery):
    h = navigation_ui; page = h['page']; seed(h)
    page.get_by_role('link', name='Alpha work', exact=True).click()
    page.locator('.wi-comment-form textarea').wait_for()
    if delivery == 'pending': h['hold'] = operation
    else: h['lost'] = operation
    if operation == 'comment':
        page.locator('.wi-comment-form textarea').fill('A stays in Alpha')
        page.locator('.wi-comment-form button[type=submit]').click()
        retry = 'Retry comment with same request'
    else:
        page.get_by_role('button', name='Edit item', exact=True).click()
        page.locator('.wi-edit input[name=title]').fill('Alpha update keeps original revision')
        page.get_by_role('button', name='Save item', exact=True).click()
        retry = 'Retry same request'
    if delivery == 'pending':
        await_held(h); held = h['held'].pop()
    else: page.get_by_role('button', name=retry, exact=True).wait_for()
    original = copy.deepcopy(h['writes'][0])
    page.get_by_role('link', name='Beta work', exact=True).click()
    page.locator('.wi-detail h3').filter(has_text='Exact beta outcome').wait_for()
    page.locator('.wi-comment-form textarea').fill('Beta draft untouched')
    if delivery == 'pending':
        h['hold'] = ''
        if operation == 'comment': h['comments'][A].append({'id': 'settled', 'text': original[1]['text']})
        else: h['items'][A].update(original[1]['changes'])
        h['items'][A]['revision'] += 1
        held.fulfill(json={'ok': True, 'item': h['items'][A]})
    page.get_by_role('link', name='Alpha work', exact=True).click()
    page.locator('.wi-item-identity').filter(has_text=A).wait_for()
    if delivery == 'unknown':
        page.get_by_role('button', name=retry, exact=True).click()
        page.wait_for_function("!document.querySelector('.wi-detail')?.textContent.includes('not confirmed')")
        assert h['writes'] == [original, original]
    else:
        page.get_by_role('button', name='Edit item', exact=True).wait_for()
        assert h['writes'] == [original]
    assert original[1]['item_id'] == A
    if operation == 'update': assert original[1]['expected_revision'] == 1
    page.get_by_role('link', name='Beta work', exact=True).click()
    page.locator('.wi-item-identity').filter(has_text=B).wait_for()
    page.locator('.wi-comment-form textarea').wait_for()
    assert page.locator('.wi-comment-form textarea').input_value() == 'Beta draft untouched'
    assert not h['frames'] and not h['errors']


@pytest.mark.parametrize('theme,width', [('dark', 1700), ('light', 390)])
def test_readable_link_destination_at_real_sizes(navigation_ui, theme, width, tmp_path):
    h = navigation_ui; page = h['page']; seed(h)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('theme=>window.ouroTheme.set(theme)', theme)
    assert page.locator('html').get_attribute('data-theme') == theme
    page.get_by_role('link', name='Alpha work', exact=True).click()
    page.locator('.wi-detail h3').filter(has_text='Exact alpha outcome').wait_for()
    assert page.locator('.wi-detail').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    out = Path(os.environ.get('OUROBOROS_UI_EVIDENCE_DIR', str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'work-item-link-{theme}-{width}.png'))
    assert not h['writes'] and not h['errors']
