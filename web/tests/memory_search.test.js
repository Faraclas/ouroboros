import assert from 'node:assert/strict';
import test from 'node:test';
import { initMemorySearch, statusTone, formatBadge } from '../modules/memory_search.js';
import { setup } from './dashboard_dom_fixture.js';

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('statusTone and formatBadge map lifecycle states cleanly', () => {
    assert.equal(statusTone('ACTIVE'), 'ok');
    assert.equal(statusTone('ACCEPTED'), 'ok');
    assert.equal(statusTone('ARCHIVED'), 'neutral');
    assert.equal(statusTone('SUPERSEDED'), 'neutral');
    assert.equal(statusTone('CLOSED'), 'neutral');
    assert.equal(
        formatBadge({
            status: 'CLOSED',
            date_ymd: '2026-11-22',
            scope: 'project:alpha',
            canonical_address: "memory_read(node_id='closure_brief:alpha')",
        }),
        "[CLOSED | 2026-11-22 | project:alpha | memory_read(node_id='closure_brief:alpha')]",
    );
});

test('initMemorySearch queries apiClient.memorySearch, renders hits, and handles empty/error states', async (t) => {
    const { mount, window } = setup(t);
    const calls = [];
    const copied = [];
    const client = {
        projectCatalog: async () => ({
            projects: [{ id: 'closed-proj', name: 'Closed Proj', lifecycle: 'tombstoned' }],
        }),
        memorySearch: async (opts) => {
            calls.push(opts);
            if (opts.query === 'unbuilt') {
                const err = new Error('Memory index is not built yet.');
                err.status = 503;
                err.body = { reason_code: 'index_not_built' };
                throw err;
            }
            if (opts.query === 'fail') throw new Error('Memory index unavailable');
            if (opts.query === 'empty') {
                return {
                    query: 'empty', scope: opts.scope, include_archived: opts.includeArchived,
                    limit: 20, hit_count: 0, elapsed_ms: 2, rebuilt: false, hits: [],
                };
            }
            return {
                query: opts.query,
                scope: opts.scope,
                include_archived: opts.includeArchived,
                limit: 20,
                hit_count: 1,
                elapsed_ms: 4,
                rebuilt: false,
                hits: [
                    {
                        doc_key: 'closure_brief:alpha',
                        family: 'project',
                        kind: 'closure_brief',
                        status: 'CLOSED',
                        scope: 'project:alpha',
                        date_ymd: '2026-11-22',
                        canonical_address: "memory_read(node_id='closure_brief:alpha')",
                        badge: "[CLOSED | 2026-11-22 | project:alpha | memory_read(node_id='closure_brief:alpha')]",
                        title: 'Alpha Closure',
                        snippet: 'Closed alpha project details',
                        score: 1.5,
                    },
                ],
            };
        },
    };
    const state = { projects: [{ id: 'alpha', name: 'Alpha' }] };
    const liveProjects = [{ id: 'alpha', name: 'Alpha' }];
    const panel = initMemorySearch({
        mount,
        client,
        getProjects: () => liveProjects,
        copyAddress: async (addr) => { copied.push(addr); return true; },
    });

    window.dispatchEvent(new CustomEvent('ouro:dashboard-subtab-shown', { detail: { tab: 'memory' } }));
    await settle();
    const scopeSelect = mount.querySelector('#memory-search-scope');
    const scopeOptions = scopeSelect.querySelectorAll('option').map((opt) => opt.getAttribute('value'));
    assert.deepEqual(scopeOptions, ['', 'global', 'project:alpha', 'project:closed-proj']);

    const input = mount.querySelector('#memory-search-input');
    input.value = 'alpha';
    await panel.search();
    await settle();

    assert.equal(calls.length, 1);
    assert.equal(calls[0].query, 'alpha');
    assert.match(mount.querySelector('#memory-search-summary').textContent, /1 match · 4 ms/);
    assert.match(mount.querySelector('#memory-search-results').textContent, /Alpha Closure/);
    assert.match(mount.querySelector('#memory-search-results').textContent, /memory_read\(node_id='closure_brief:alpha'\)/);

    const copyBtn = mount.querySelector('[data-copy-hit]');
    await mount.querySelector('#memory-search-results').fire('click', { target: copyBtn });
    await settle();
    assert.deepEqual(copied, ["memory_read(node_id='closure_brief:alpha')"]);
    assert.equal(copyBtn.textContent, 'Copied');

    await panel.search('empty');
    assert.match(mount.querySelector('#memory-search-results').textContent, /No matching memory records/);

    await panel.search('fail');
    assert.equal(mount.querySelector('#memory-search-status').dataset.tone, 'error');
    assert.match(mount.querySelector('#memory-search-status').textContent, /Memory index unavailable/);
    assert.ok(mount.querySelector('[data-memory-retry]'));

    await panel.search('unbuilt');
    assert.equal(mount.querySelector('#memory-search-status').dataset.tone, 'neutral');
    assert.match(mount.querySelector('#memory-search-status').textContent, /not built yet/);
    assert.equal(mount.querySelector('[data-memory-retry]'), null);

    panel.destroy();
});
