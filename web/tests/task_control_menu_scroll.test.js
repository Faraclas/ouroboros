import assert from 'node:assert/strict';
import test from 'node:test';
import { closeTaskControlMenu, openTaskControlMenu, ACTION_STOP_NOW } from '../modules/task_control_menu.js';
import { installDomDouble } from './dom_double.js';

function fixture(t) {
    const env = installDomDouble();
    const prior = globalThis.IntersectionObserver;
    let observer;
    globalThis.IntersectionObserver = class {
        constructor(callback) { observer = callback; }
        observe() {}
        takeRecords() { return []; }
        disconnect() {}
    };
    t.after(() => { closeTaskControlMenu(); globalThis.IntersectionObserver = prior; env.restore(); });
    const anchor = env.el('button'), feed = env.el('div');
    env.doc.body.append(anchor, feed);
    const actions = [];
    const menu = openTaskControlMenu(anchor, { actions: [ACTION_STOP_NOW], onAction: a => actions.push(a) });
    return { ...env, anchor, feed, menu, actions, observe: entries => observer(entries) };
}

test('queued or independent scroll keeps a stationary task-menu anchor usable', t => {
    const f = fixture(t);
    f.feed.emit('scroll');
    assert.ok(f.menu.isConnected);
    assert.equal(f.anchor.getAttribute('aria-expanded'), 'true');
    assert.deepEqual(f.actions, []);
    f.menu.querySelector('button').click();
    assert.deepEqual(f.actions, [ACTION_STOP_NOW]);
    assert.ok(!f.menu.isConnected);
});

test('actual anchor movement dismisses without a task action and releases watchers', t => {
    const f = fixture(t);
    f.anchor.rect.top += 2;
    f.anchor.rect.bottom += 2;
    f.feed.emit('scroll');
    assert.ok(!f.menu.isConnected);
    assert.equal(f.anchor.getAttribute('aria-expanded'), 'false');
    assert.deepEqual(f.actions, []);
    assert.equal(f.doc.captures.get('scroll').size, 0);
    assert.equal(window.listeners.get('resize').size, 0);
});

test('the height-clamped menu keeps its own scroll usable', t => {
    const f = fixture(t);
    f.menu.emit('scroll');
    assert.ok(f.menu.isConnected);
    assert.deepEqual(f.actions, []);
});

test('resize still dismisses without a task action', t => {
    const f = fixture(t);
    window.emit('resize');
    assert.ok(!f.menu.isConnected);
    assert.deepEqual(f.actions, []);
});

test('a disconnected anchor still dismisses without a task action', t => {
    const f = fixture(t);
    f.anchor.remove();
    f.observe([{ target: f.anchor, isIntersecting: false }]);
    assert.ok(!f.menu.isConnected);
    assert.deepEqual(f.actions, []);
});
