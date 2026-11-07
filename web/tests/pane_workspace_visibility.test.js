import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the actual sync function against its explicit closure collaborators.
// Hidden/inert facts here are not a browser geometry or animation assertion.
const source = fs.readFileSync(new URL('../modules/pane_workspace.js', import.meta.url), 'utf8');
const syncSource = source.slice(source.indexOf('    function sync() {'), source.indexOf('    function closeOne('));
function node() {
    const classes = new Map(), attrs = new Map();
    return { hidden: false, inert: false, clientWidth: 390,
        classList: { toggle(name, value) { classes.set(name, value); } },
        style: { setProperty() {} }, setAttribute(name, value) { attrs.set(name, value); },
        toggleAttribute(name, value) { if (value) attrs.set(name, ''); else attrs.delete(name); },
        hasAttribute: name => attrs.has(name),
    };
}
function fixture({ leaving = true, kept = false } = {}) {
    const oldPage = { hidden: false, hasAttribute: name => name === 'data-leaving' && leaving };
    const make = page => ({ el: node(), body: { querySelector: selector => selector === '.chat-instance-panel' ? page : null },
        tab: node(), title: { textContent: 'Fixture room' } });
    const old = make(oldPage), current = make({ hidden: false, hasAttribute: () => false });
    const sandbox = { pair: kept ? ['old', 'current'] : ['current'], collaboration: false, narrow: false,
        active: () => 'current', lastFocused: null, weight: () => 1, workspace: { ...node(), clientWidth: 390 },
        panel: node(), content: node(), tabs: { ...node(), children: [null, current.tab], insertBefore() {} },
        mainTab: node(), body: node(), hosts: new Map([['old', old], ['current', current]]),
        dividers: new Map(), divider: () => node(), document: { documentElement: { dataset: { layout: 'stack' } }, body: node() },
    };
    vm.runInNewContext(syncSource + '\nsync();', sandbox);
    return { old, oldPage, current };
}

test('pane sync retains an outgoing retired host and page while keeping them inert', () => {
    const { old, oldPage, current } = fixture();
    assert.equal(old.el.hidden, false, 'retirement must not disappear under a hidden ancestor');
    assert.equal(oldPage.hidden, false);
    assert.equal(old.el.inert, true, 'visible exit is not an interactive/readable live room');
    assert.equal(old.el.hasAttribute('data-leaving'), true);
    assert.equal(old.tab.hidden, true, 'retirement does not become an extra open-room tab');
    assert.equal(current.el.hidden, false);
    assert.equal(current.el.inert, false);
});

test('ordinary hidden hosts stay hidden and a reclaimed host sheds the exit marker', () => {
    const hidden = fixture({ leaving: false });
    assert.equal(hidden.old.el.hidden, true);
    assert.equal(hidden.oldPage.hidden, true);
    const reclaimed = fixture({ leaving: false, kept: true });
    assert.equal(reclaimed.old.el.hidden, false);
    assert.equal(reclaimed.old.el.inert, false);
    assert.equal(reclaimed.old.el.hasAttribute('data-leaving'), false);
});
