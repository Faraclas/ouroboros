import assert from 'node:assert/strict';
import test from 'node:test';
import { installDomDouble } from './dom_double.js';
import { initEvolution } from '../modules/evolution.js';
import { WS } from '../modules/ws.js';

test('equal campaign snapshots retain the readable fact paragraph', () => {
    const env = installDomDouble();
    const oldFetch = globalThis.fetch;
    globalThis.fetch = () => { throw Error('This synchronous snapshot test makes no requests'); };
    let controller;
    try {
        const mount = env.doc.createElement('div'); env.doc.body.append(mount);
        controller = initEvolution({ ws: new WS('ws://unused'), mount,
            state: { activePage: 'chat', dashboardActiveSubtab: 'evolution' } });
        const snapshot = { runtime_mode: 'pro', evolution_enabled: true,
            evolution_state: { status: 'running', enabled: true,
                campaign: { objective: 'Retain readable evidence', progress_notes: 'Checking the source' } } };
        controller.hydrateStateSnapshot(snapshot);
        const lines = mount.querySelector('.evo-campaign-lines');
        const first = lines.children[0]; const second = lines.children[1];
        assert.match(first.textContent, /Retain readable evidence/);
        controller.hydrateStateSnapshot(structuredClone(snapshot));
        assert.equal(lines.children[0], first, 'equal reads cannot detach a selected/read paragraph');
        assert.equal(lines.children[1], second);
        controller.hydrateStateSnapshot({ ...snapshot, evolution_state: {
            ...snapshot.evolution_state, campaign: { ...snapshot.evolution_state.campaign, objective: 'Updated evidence' } } });
        assert.match(lines.children[0].textContent, /Updated evidence/);
        assert.equal(lines.children[1], second, 'an unchanged sibling fact retains identity too');
    } finally { controller?.dispose(); globalThis.fetch = oldFetch; env.restore(); }
});
