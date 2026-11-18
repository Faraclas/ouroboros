import assert from 'node:assert/strict';
import test from 'node:test';
import { workItemHref, workItemId, bindWorkItemReferences } from '../modules/work_item_reference.js';
import { chatMarkdownUrl } from '../modules/chat_markdown.js';

const ID = '36c87df8-8b24-4767-80a9-507c1b7e9f68', HREF = `/?work_item=${ID}`;

test('one explicit UUID contract, not arbitrary paths, schemes or association text', () => {
    assert.equal(workItemHref(ID.toUpperCase()), HREF);
    assert.equal(workItemId(HREF), ID);
    assert.equal(workItemId(`https://local.test${HREF}`, 'https://local.test/'), ID);
    for (const bad of ['', null, 'title', `#work-item=${ID}`, `?work_item=${ID}`,
        `/other${HREF}`, HREF + '&q=other', HREF + '&work_item=' + ID, HREF + '#settings',
        HREF + ' ', HREF.replace(ID, 'not-a-uuid'), '//remote.test' + HREF,
        'https://remote.test' + HREF, 'https://user@local.test' + HREF,
        '/\\remote.test' + HREF, 'javascript:alert(1)', '/api/extensions/project-boards/item?item_id=' + ID]) {
        assert.equal(workItemId(bad, 'https://local.test/'), '', String(bad));
    }
    assert.equal(workItemHref('not-a-uuid'), '');
});

test('both render policies retain authored labels and admit only the explicit internal URL', () => {
    assert.equal(chatMarkdownUrl(HREF), HREF);
    assert.equal(chatMarkdownUrl('/?work_item=title'), '');
});

function fixture(url = 'https://local.test/') {
    const doc = new EventTarget(), calls = [], errors = [], attrs = new Map([['href', HREF]]);
    const anchor = { closest: () => anchor, getAttribute: name => attrs.get(name),
        hasAttribute: name => attrs.has(name), setAttribute: (name, value) => attrs.set(name, value),
        removeAttribute: name => attrs.delete(name) };
    const dispose = bindWorkItemReferences({ doc, win: { location: { href: url } },
        openItem: (id, options) => new Promise((resolve, reject) => calls.push({ id, ...options, resolve, reject })),
        onError: error => errors.push(error.message) });
    const click = (options = {}) => {
        const event = new Event('click', { cancelable: true });
        Object.defineProperty(event, 'target', { value: anchor });
        Object.assign(event, { button: 0, ...options }); doc.dispatchEvent(event); return event;
    };
    return { doc, calls, errors, attrs, click, dispose };
}

test('render/startup is inert; deliberate clicks are bounded and later intent cancels', async () => {
    const f = fixture();
    assert.equal(f.calls.length, 0);
    for (const option of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
        assert.equal(f.click(option).defaultPrevented, false);
    }
    assert.equal(f.calls.length, 0);
    assert.equal(f.click().defaultPrevented, true);
    assert.equal(f.calls[0].id, ID); assert.equal(f.attrs.get('aria-busy'), 'true');
    f.click(); assert.equal(f.calls[0].signal.aborted, true);
    f.calls[0].resolve(); await Promise.resolve();
    assert.equal(f.attrs.get('aria-busy'), 'true', 'old completion cannot clear the new navigation status');
    f.doc.dispatchEvent(new Event('keydown')); assert.equal(f.calls[1].signal.aborted, true);
    f.calls[1].reject(Error('aborted old read')); await Promise.resolve();
    assert.deepEqual(f.errors, []);
    f.dispose(); f.click(); assert.equal(f.calls.length, 2);
});

test('only explicit link startup reads; real failure is reported and disposal aborts', async () => {
    const f = fixture('https://local.test' + HREF);
    assert.equal(f.calls.length, 1);
    f.calls[0].reject(Error('Item missing')); await Promise.resolve();
    assert.deepEqual(f.errors, ['Item missing']);
    f.click(); f.dispose(); assert.equal(f.calls[1].signal.aborted, true);
    f.calls[1].resolve(); await Promise.resolve();
    assert.equal(f.attrs.has('aria-busy'), false);
});
