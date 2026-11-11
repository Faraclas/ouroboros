import assert from 'node:assert/strict';
import test from 'node:test';
import { installDom, restoreDom } from './chat_dom_fixture.js';
import { paintWaitDependencies, paintWaitBack } from '../modules/wait_dependencies.js';

const work = () => ({ work_id: 'a', name: 'Install', room: 1, confirmed: true, because: [{ code: 'sleeping',
    dependency: { waiting_for: 'Tests', why: '<script>not html</script>', release: 'Owner confirms', on: ['b'] },
    targets: [{ task_id: 'b', work_id: 'b', project_id: 'preview', name: 'Preview tests', room: 12,
        state: 'live', activity: 'waiting', selected_as_wake: false }] }] });
const text = node => node.textContent + [...node.children].map(text).join('');

test('authored explanation is text; links carry identity and a return trail, never controls', () => {
    const { prior, mount } = installDom();
    try {
        const node = document.createElement('div'); mount.append(node);
        paintWaitDependencies(node, work());
        assert.match(text(node), /Owner confirms/);
        assert.match(text(node), /not an automatic release check/);
        assert.match(text(node), /Not explicitly selected/);
        assert.equal(node.querySelector('script'), null);
        const button = node.querySelector('[data-intent="open-project"]');
        assert.ok(button);
        let event;
        window.dispatchEvent = e => { event = e.detail; };
        button.click();
        assert.equal(event.project.id, 'preview');
        assert.equal(event.project.chat_id, 12, 'a target outside the recent project list still carries its proven room');
        assert.equal(event.work_id, 'b');
        assert.equal(event.wait_trail[0].work_id, 'a');
        assert.equal(node.querySelector('[data-action="resume"]'), null);
        paintWaitDependencies(node, { ...work(), confirmed: false });
        assert.equal(node.hidden, true);
    } finally { restoreDom(prior); }
});

test('ended/unknown targets have no invented room link or satisfied release, old sleeps explain the gap', () => {
    const { prior, mount } = installDom();
    try {
        const node = document.createElement('div'); mount.append(node);
        const value = work(); value.because[0].targets[0].state = 'ended';
        paintWaitDependencies(node, value);
        assert.match(text(node), /release condition not evaluated/);
        assert.equal(node.querySelector('button'), null);
        paintWaitDependencies(node, { confirmed: true, because: [{ code: 'sleeping' }] });
        assert.match(text(node), /No operation explanation/);
        paintWaitBack(node, []); assert.equal(node.hidden, true);
    } finally { restoreDom(prior); }
});

test('target status refresh keeps the exact focused link node', () => {
    const { prior, mount } = installDom();
    try {
        const node = document.createElement('div'); mount.append(node);
        const value = work();
        paintWaitDependencies(node, value);
        const button = node.querySelector('button'); button.focus();
        value.because[0].targets[0].activity = 'working';
        paintWaitDependencies(node, value);
        assert.equal(node.querySelector('button'), button);
        assert.equal(document.activeElement, button);
        assert.match(text(node), /Working/);
    } finally { restoreDom(prior); }
});
