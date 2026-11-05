import test from 'node:test';
import assert from 'node:assert/strict';
import { apiClient } from '../modules/api_client.js';

// Exercise the real transport adapter: omitted parent and explicit root differ.
test('project catalog preserves root/immediate-parent filters and ordinary pagination', async () => {
    const original = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (url, init) => {
        calls.push({url: String(url), init});
        return {ok: true, json: async () => ({projects: [], total: 0})};
    };
    try {
        await apiClient.projectCatalog();
        await apiClient.projectCatalog({parent_id: '', q: 'a & b', offset: 2, limit: 3});
        await apiClient.projectCatalog({parent_id: 'a.b-c', lifecycle: 'closed'});
        assert.equal(new URL(calls[0].url, 'http://local').searchParams.has('parent_id'), false);
        const root = new URL(calls[1].url, 'http://local').searchParams;
        assert.equal(root.has('parent_id'), true);
        assert.equal(root.get('parent_id'), '');
        assert.equal(root.get('q'), 'a & b');
        assert.equal(root.get('offset'), '2');
        assert.equal(root.get('limit'), '3');
        assert.equal(new URL(calls[2].url, 'http://local').searchParams.get('parent_id'), 'a.b-c');
        assert.equal(new URL(calls[2].url, 'http://local').searchParams.get('lifecycle'), 'closed');
        assert.ok(calls.every(call => call.init.cache === 'no-store'));
        await apiClient.projectUpdate('a.b-c', {parent_id: ''});
        assert.equal(calls[3].url, '/api/projects/a.b-c/update');
        assert.deepEqual(JSON.parse(calls[3].init.body), {parent_id: ''});
    } finally {
        globalThis.fetch = original;
    }
});
