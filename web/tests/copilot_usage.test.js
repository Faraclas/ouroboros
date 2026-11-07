import assert from 'node:assert/strict';
import test from 'node:test';
import { bindCopilotHeader, usagePresentation, usageSnapshot } from '../modules/copilot_usage.js';
import { installDomDouble } from './dom_double.js';

const now = Date.parse('2030-01-01T00:00:00Z');
const snapshot = (pct, extra = {}) => ({ schema_version: 1, status: 'fresh',
    observed_at: new Date(now).toISOString(), premium: { used_percentage: pct, unlimited: false }, ...extra });

for (const [pct, label, tone] of [[0, '0%', 'normal'], [90, '90%', 'normal'],
    [90.1, '90%', 'warning'], [95, '95%', 'warning'], [95.1, '95%', 'danger']]) {
    test(`Copilot ${pct}% chooses tone before rounding`, () => {
        const view = usagePresentation(snapshot(pct), now);
        assert.equal(view.label, label); assert.equal(view.tone, tone); assert.equal(view.fill, pct);
    });
}

test('unavailable, stale, invalid and unlimited account snapshots never pretend fresh zero', () => {
    for (const data of [{ schema_version: 1, status: 'unavailable' }, snapshot(-1), snapshot(101)]) {
        const view = usagePresentation(data, now);
        assert.deepEqual([view.label, view.tone, view.fill], ['—', 'neutral', null]);
    }
    const stale = usagePresentation(snapshot(96, { status: 'stale' }), now);
    assert.deepEqual([stale.label, stale.tone, stale.stale], ['96%', 'neutral', true]);
    assert.equal(usageSnapshot(snapshot(50), now + 25 * 60 * 60_000).status, 'unavailable');
    assert.deepEqual([usagePresentation(snapshot(null, { premium: { unlimited: true } }), now).label,
        usagePresentation(snapshot(null, { premium: { unlimited: true } }), now).fill], ['Unlimited', null]);
});

test('actual Copilot header binding paints only account snapshots and disposes its subscription', () => {
    const f = installDomDouble();
    let paint, released = 0;
    try {
        const pill = f.el('button', {}, f.el('span', { className: 'chat-budget-text' }),
            f.el('span', { className: 'copilot-usage-stale' }), f.el('div', { className: 'chat-budget-bar' }));
        const dispose = bindCopilotHeader(pill, { subscribe(callback) { paint = callback; return () => { released++; }; } });
        paint({ schema_version: 1, status: 'unavailable' });
        assert.equal(pill.querySelector('.chat-budget-text').textContent, '—');
        assert.equal(pill.dataset.usageTone, 'neutral');
        assert.match(pill.getAttribute('aria-label'), /unavailable/);
        // Use the real clock only to make this a fresh consumer snapshot.
        paint(snapshot(91, { observed_at: new Date().toISOString() }));
        assert.equal(pill.querySelector('.chat-budget-text').textContent, '91%');
        assert.equal(pill.dataset.usageTone, 'warning');
        assert.equal(pill.querySelector('.chat-budget-bar').getAttribute('aria-valuenow'), '91');
        assert.equal(pill.css.get('--budget-fill'), '91%');
        assert.match(pill.getAttribute('aria-label'), /across GitHub Copilot apps/);
        assert.equal(pill.dataset.attention, undefined, 'account usage is not app-budget action authority');
        dispose(); assert.equal(released, 1);
    } finally { f.restore(); }
});
