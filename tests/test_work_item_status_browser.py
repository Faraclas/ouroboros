"""Direct authored status in the normal SPA; controlled CAS/receipts, no owner writes."""
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
STATES = {'idea': 'Idea', 'planned': 'Planned', 'done': 'Done', 'set_aside': 'Set aside'}


def open_board(h, known_link=False):
    def listing(route):
        q = parse_qs(urlparse(route.request.url).query)
        population = [v for v in h['items'].values() if v['project_id'] == q['project_id'][0]]
        counts = {key: sum(v['disposition'] == key for v in population) for key in STATES}
        selected = q.get('disposition', [''])[0]
        items = [v for v in population if not selected or v['disposition'] == selected]
        route.fulfill(json={'ok': True, 'items': items, 'counts': counts,
            'total': len(items), 'offset': 0, 'limit': 100, 'has_more': False})
    h['page'].context.route('**/api/extensions/project-boards/list?*', listing)
    if known_link:
        from tests.test_work_item_reference_browser import seed
        seed(h)
        h['page'].get_by_role('link', name='Alpha work', exact=True).click()
        h['page'].locator('[data-wi-status]').wait_for()
    else:
        dashboard_board(h)


def settled(page, state):
    page.wait_for_function("s=>document.querySelector('[data-wi-status]')?.value===s && !document.querySelector('[data-wi-status]').disabled", arg=state)
    page.wait_for_function("!document.querySelector('.wi-list[aria-busy=true]')")


def test_direct_status_only_updates_field_counts_and_survives_reload(navigation_ui):
    h = navigation_ui; page = h['page']; open_board(h)
    page.locator('.wi-comment-form textarea').fill('Unsent discussion survives status changes')
    for index, state in enumerate(['planned', 'done', 'set_aside', 'idea'], 1):
        page.locator('[data-wi-status]').select_option(state); settled(page, state)
        assert page.locator('[data-wi-item=alpha] .wi-disposition').inner_text() == STATES[state]
        assert page.locator(f'[data-wi-count={state}]').inner_text() == '1'
        assert page.locator('.wi-comment-form textarea').input_value() == 'Unsent discussion survives status changes'
        payload = h['writes'][-1][1]
        assert payload['changes'] == {'disposition': state} and payload['expected_revision'] == index
    assert len({p['request_id'] for _, p in h['writes']}) == 4
    page.reload(); dashboard_board(h); settled(page, 'idea')
    assert len(h['writes']) == 4 and not h['frames'] and not h['errors']


def test_filtered_item_leaves_list_but_detail_remains_changeable(navigation_ui):
    h = navigation_ui; page = h['page']; open_board(h)
    page.locator('[data-wi-filter=idea]').click()
    page.locator('[data-wi-item=alpha]').wait_for()
    page.locator('[data-wi-status]').select_option('done'); settled(page, 'done')
    assert page.locator('[data-wi-item=alpha]').count() == 0
    assert page.locator('[data-wi-count=done]').inner_text() == '1'
    assert page.locator('.wi-detail h3').inner_text() == 'Discuss alpha'
    page.locator('[data-wi-filter=done]').click()
    page.locator('[data-wi-item=alpha]').wait_for()
    assert not h['errors']


def test_open_editor_keeps_its_draft_and_original_revision(navigation_ui):
    h = navigation_ui; page = h['page']; open_board(h)
    page.get_by_role('button', name='Edit item', exact=True).click()
    page.locator('.wi-edit input[name=title]').fill('Keep this draft')
    assert page.locator('[data-wi-status]').is_disabled()
    page.locator('.wi-edit select[name=disposition]').select_option('planned')
    assert not h['writes']
    page.get_by_role('button', name='Save item', exact=True).click(); settled(page, 'planned')
    assert h['writes'][0][1]['expected_revision'] == 1
    assert h['items']['alpha']['title'] == 'Keep this draft'


def test_conflict_retry_changes_only_status_not_concurrent_text(navigation_ui):
    h = navigation_ui; page = h['page']; open_board(h)
    h['items']['alpha'].update(revision=2, title='Concurrent title', description='Concurrent words')
    page.locator('[data-wi-status]').select_option('done')
    page.get_by_role('button', name='Apply status to current revision', exact=True).wait_for()
    assert h['items']['alpha']['disposition'] == 'idea'
    page.get_by_role('button', name='Apply status to current revision', exact=True).click(); settled(page, 'done')
    assert h['writes'][1][1]['changes'] == {'disposition': 'done'}
    assert h['writes'][1][1]['expected_revision'] == 2
    assert h['items']['alpha']['description'] == 'Concurrent words'
    assert page.locator('.wi-detail h3').inner_text() == 'Concurrent title'


@pytest.mark.parametrize('delivery', ['pending', 'unknown'])
def test_status_request_keeps_identity_and_destination_across_project_switch(navigation_ui, delivery):
    h = navigation_ui; page = h['page']; open_board(h)
    if delivery == 'pending': h['hold'] = 'update'
    else: h['lost'] = 'update'
    page.locator('[data-wi-status]').select_option('done')
    if delivery == 'pending': await_held(h); held = h['held'].pop()
    else: page.get_by_role('button', name='Retry same request', exact=True).wait_for()
    original = copy.deepcopy(h['writes'][0])
    page.locator('[data-pm-row=beta]').click()
    page.locator('[data-wi-item=beta]').click()
    page.locator('.wi-comment-form textarea').fill('Beta draft')
    if delivery == 'pending':
        h['hold'] = ''; h['items']['alpha'].update(disposition='done', revision=2)
        held.fulfill(json={'ok': True, 'item': h['items']['alpha']})
    page.locator('[data-pm-row=alpha]').click()
    if delivery == 'unknown':
        page.get_by_role('button', name='Retry same request', exact=True).click()
    settled(page, 'done')
    assert h['writes'] == ([original, original] if delivery == 'unknown' else [original])
    page.locator('[data-pm-row=beta]').click()
    page.locator('.wi-comment-form textarea').wait_for()
    assert page.locator('.wi-comment-form textarea').input_value() == 'Beta draft'
    assert not h['frames'] and not h['errors']


@pytest.mark.parametrize('theme,width', [('dark', 3520), ('light', 1700), ('dark', 390), ('light', 390)])
def test_status_colors_copy_spacing_and_narrow_fit(navigation_ui, theme, width, tmp_path):
    h = navigation_ui; page = h['page']; open_board(h, known_link=True)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('t=>window.ouroTheme.set(t)', theme)
    colors = {}
    for state in STATES:
        page.locator('[data-wi-status]').select_option(state); settled(page, state)
        colors[state] = page.locator('[data-wi-status]').evaluate('n=>getComputedStyle(n).color')
        if state != 'set_aside':
            out = Path(os.environ.get('OUROBOROS_UI_EVIDENCE_DIR', str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
            page.locator('.work-items').screenshot(path=str(out / f'status-{state}-{theme}-{width}.png'))
    assert len(set(colors.values())) == 4
    geometry = page.locator('.wi-item-identity .wi-actions').evaluate('n=>{const [a,b]=[...n.children].map(x=>x.getBoundingClientRect());return {gap:b.left-a.right,vertical:b.top-a.bottom,same:a.top===b.top}}')
    assert (geometry['gap'] if geometry['same'] else geometry['vertical']) >= 7
    assert page.locator('.wi-detail').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    assert page.locator('.wi-detail').evaluate('n=>n.getBoundingClientRect().right<=innerWidth')
    assert page.locator('html').get_attribute('data-theme') == theme
    out = Path(os.environ.get('OUROBOROS_UI_EVIDENCE_DIR', str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'status-{theme}-{width}.png'))
    assert not h['errors']


@pytest.mark.parametrize('delivery', ['pending', 'unknown'])
def test_conflict_controls_cannot_discard_unresolved_status_request(navigation_ui, delivery):
    h = navigation_ui; page = h['page']; open_board(h)
    h['items']['alpha'].update(revision=2, description='First concurrent text')
    page.locator('[data-wi-status]').select_option('done')
    apply = page.get_by_role('button', name='Apply status to current revision', exact=True)
    keep = page.get_by_role('button', name='Keep current status', exact=True)
    apply.wait_for()
    if delivery == 'pending': h['hold'] = 'update'
    else: h['lost'] = 'update'
    apply.click()
    if delivery == 'pending': await_held(h); held = h['held'].pop()
    else: page.get_by_role('button', name='Retry same request', exact=True).wait_for()
    assert apply.is_disabled() and keep.is_disabled()
    # Native disabled buttons must not erase the discriminator, even for .click().
    keep.evaluate('n=>n.click()')
    if delivery == 'pending':
        h['hold'] = ''; h['items']['alpha'].update(revision=3, description='Second concurrent text')
        held.fulfill(status=409, json={'ok': False, 'error': {'code': 'revision_conflict'}, 'current': h['items']['alpha']})
        page.wait_for_function("!document.querySelector('.wi-conflict button').disabled")
        apply.click()
    else:
        page.get_by_role('button', name='Retry same request', exact=True).click()
    settled(page, 'done')
    assert all(payload['changes'] == {'disposition': 'done'} for _, payload in h['writes'])
    assert h['items']['alpha']['description'] == ('Second concurrent text' if delivery == 'pending' else 'First concurrent text')
    if delivery == 'unknown': assert h['writes'][1] == h['writes'][2]
    else: assert h['writes'][-1][1]['expected_revision'] == 3
    assert not h['errors']
