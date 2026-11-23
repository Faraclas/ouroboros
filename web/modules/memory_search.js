// Dashboard -> Memory: thin read-only search over GET /api/memory/search (`state/memory_index.sqlite`),
// surfacing `[STATUS | YYYY-MM-DD | scope | canonical_address]` badges and copyable canonical addresses.

import { apiClient } from './api_client.js';
import { copyTextWithToast } from './ui_helpers.js';
import { escapeHtmlAttr, escapeHtmlText } from './utils.js';

/**
 * @typedef {Object} MemorySearchHit
 * @property {string} doc_key
 * @property {string} family
 * @property {string} kind
 * @property {string} status
 * @property {string} scope
 * @property {string} date_ymd
 * @property {string} canonical_address
 * @property {string} badge
 * @property {string} title
 * @property {string} snippet
 * @property {number} score
 */
/**
 * @typedef {Object} MemorySearchResponse
 * @property {string} query
 * @property {string} scope
 * @property {boolean} include_archived
 * @property {number} limit
 * @property {number} hit_count
 * @property {number} elapsed_ms
 * @property {boolean} rebuilt
 * @property {Array<MemorySearchHit>} hits
 */

const text = escapeHtmlText;
const attr = escapeHtmlAttr;

/** Map indexed record status to shared DESIGN.md semantic tone tokens. */
export function statusTone(status) {
    const upper = String(status || '').trim().toUpperCase();
    if (upper === 'ACTIVE' || upper === 'ACCEPTED') return 'ok';
    return 'neutral';
}

export function formatBadge(hit) {
    if (hit && typeof hit.badge === 'string' && hit.badge.trim()) return hit.badge.trim();
    const status = String(hit?.status || 'UNKNOWN');
    const date = String(hit?.date_ymd || 'unknown');
    const scope = String(hit?.scope || 'global');
    const addr = String(hit?.canonical_address || '');
    return `[${status} | ${date} | ${scope} | ${addr}]`;
}

function buildScopeOptions(projects) {
    const list = [
        { value: '', label: 'All memory' },
        { value: 'global', label: 'Global knowledge & marks' },
    ];
    const seen = new Set(['', 'global']);
    const rows = Array.isArray(projects) ? projects : [];
    for (const proj of rows) {
        const pid = String(proj?.id || '').trim();
        if (!pid) continue;
        const scope = `project:${pid}`;
        if (seen.has(scope)) continue;
        seen.add(scope);
        const name = String(proj?.name || pid).trim();
        list.push({ value: scope, label: name === pid ? scope : `${name} (${pid})` });
    }
    return list;
}

export function initMemorySearch({
    mount,
    client = apiClient,
    getProjects = () => [],
    copyAddress = (addr) => copyTextWithToast(addr, { okMessage: 'Address copied.' }),
} = {}) {
    if (!mount) return { destroy() {}, search: async () => null };

    mount.innerHTML = `
        <div class="memory-search-root">
            <div class="memory-search-scroll scroll-fade-y">
                <section class="memory-search-card" aria-labelledby="memory-search-heading">
                    <div class="memory-search-head">
                        <h3 id="memory-search-heading" class="memory-search-title">Memory Search</h3>
                        <p class="memory-search-desc">Search distilled knowledge notes, chronicle records, workpads, journals, closure briefs, and active marks.</p>
                    </div>
                    <form class="memory-search-form" data-memory-search-form>
                        <div class="memory-search-bar">
                            <input id="memory-search-input" class="ui-control memory-search-input" type="search"
                                aria-label="Search memory" placeholder="Search decisions, commits, paths, error signatures…"
                                autocomplete="off" spellcheck="false">
                            <select id="memory-search-scope" class="ui-control memory-search-scope" aria-label="Memory scope"></select>
                            <button type="submit" class="btn btn-default memory-search-submit">Search</button>
                        </div>
                        <div class="memory-search-options">
                            <label class="memory-search-toggle" for="memory-search-archived">
                                <input id="memory-search-archived" class="ui-checkbox" type="checkbox">
                                <span>Include archived &amp; superseded</span>
                            </label>
                            <span id="memory-search-summary" class="memory-search-summary" role="status" aria-live="polite"></span>
                        </div>
                    </form>
                </section>
                <div id="memory-search-status" class="ui-status" data-tone="neutral" hidden></div>
                <div id="memory-search-results" class="memory-search-results">
                    <p class="memory-search-empty">Enter keywords, commit SHAs, paths, or error signatures to search indexed memory.</p>
                </div>
            </div>
        </div>
    `;

    const form = mount.querySelector('[data-memory-search-form]');
    const input = mount.querySelector('#memory-search-input');
    const scopeSelect = mount.querySelector('#memory-search-scope');
    const archivedCheckbox = mount.querySelector('#memory-search-archived');
    const summaryEl = mount.querySelector('#memory-search-summary');
    const statusEl = mount.querySelector('#memory-search-status');
    const resultsEl = mount.querySelector('#memory-search-results');

    let requestSeq = 0;
    let activeAbort = null;
    let lastQuery = '';
    let lastHits = [];
    let catalogRows = [];
    let catalogAbort = null;
    const copyTimers = new Set();

    function syncScopeOptions() {
        const previous = scopeSelect.value || '';
        const liveRows = Array.isArray(getProjects?.()) ? getProjects() : [];
        const options = buildScopeOptions([...liveRows, ...catalogRows]);
        scopeSelect.innerHTML = options
            .map((opt) => `<option value="${attr(opt.value)}">${text(opt.label)}</option>`)
            .join('');
        if (options.some((opt) => opt.value === previous)) {
            scopeSelect.value = previous;
        }
    }

    async function refreshProjectScopes() {
        syncScopeOptions();
        if (typeof client?.projectCatalog !== 'function') return;
        if (catalogAbort) catalogAbort.abort();
        catalogAbort = typeof AbortController === 'function' ? new AbortController() : null;
        try {
            const data = await client.projectCatalog({ lifecycle: 'all', limit: 100 });
            if (catalogAbort?.signal?.aborted) return;
            if (Array.isArray(data?.projects)) {
                catalogRows = data.projects;
                syncScopeOptions();
            }
        } catch {
            // Keep live active rooms if catalog read fails.
        }
    }

    function setStatus(message, tone = 'neutral', { retry = false } = {}) {
        if (!message) {
            statusEl.hidden = true;
            statusEl.innerHTML = '';
            return;
        }
        statusEl.hidden = false;
        statusEl.dataset.tone = tone;
        statusEl.innerHTML = `<span>${text(message)}</span>${retry
            ? ' <button type="button" class="btn btn-default btn-sm" data-memory-retry>Retry</button>'
            : ''}`;
        if (retry) {
            statusEl.querySelector('[data-memory-retry]')?.addEventListener('click', () => {
                runSearch(lastQuery || input.value);
            });
        }
    }

    function renderHits(data) {
        const hits = Array.isArray(data?.hits) ? data.hits : [];
        lastHits = hits;
        const total = Number(data?.hit_count ?? hits.length);
        const elapsed = Number(data?.elapsed_ms ?? 0);
        summaryEl.textContent = `${total} ${total === 1 ? 'match' : 'matches'} · ${elapsed} ms`;
        if (!hits.length) {
            resultsEl.innerHTML = `<p class="memory-search-empty">No matching memory records for “${text(data?.query || '')}”.</p>`;
            return;
        }
        resultsEl.innerHTML = hits.map((hit, idx) => {
            const tone = statusTone(hit?.status);
            const title = String(hit?.title || hit?.canonical_address || 'Memory entry');
            const kind = String(hit?.kind || '');
            const statusLabel = String(hit?.status || 'UNKNOWN');
            const dateLabel = String(hit?.date_ymd || 'unknown');
            const scopeLabel = String(hit?.scope || 'global');
            const addr = String(hit?.canonical_address || '');
            const snippet = String(hit?.snippet || '');
            return `
                <article class="memory-hit-card" data-hit-index="${idx}">
                    <div class="memory-hit-head">
                        <h4 class="memory-hit-title">${text(title)}</h4>
                        ${kind ? `<span class="memory-hit-kind">${text(kind)}</span>` : ''}
                    </div>
                    <div class="memory-hit-meta">
                        <span class="memory-hit-status" data-tone="${attr(tone)}">${text(statusLabel)}</span>
                        <span class="memory-hit-meta-item">${text(dateLabel)}</span>
                        <span class="memory-hit-meta-item">${text(scopeLabel)}</span>
                    </div>
                    ${snippet ? `<p class="memory-hit-snippet">${text(snippet)}</p>` : ''}
                    <div class="memory-hit-foot">
                        <code class="memory-hit-address">${text(addr)}</code>
                        <button type="button" class="btn btn-default btn-sm memory-hit-copy" data-copy-hit="${idx}">Copy address</button>
                    </div>
                </article>
            `;
        }).join('');
    }

    async function runSearch(rawQuery = input.value) {
        const query = String(rawQuery || '').trim();
        lastQuery = query;
        if (!query) {
            summaryEl.textContent = '';
            setStatus('');
            resultsEl.innerHTML = '<p class="memory-search-empty">Enter keywords, commit SHAs, paths, or error signatures to search indexed memory.</p>';
            return null;
        }
        if (activeAbort) activeAbort.abort();
        activeAbort = typeof AbortController === 'function' ? new AbortController() : null;
        const seq = ++requestSeq;
        setStatus('Searching memory…', 'neutral');
        try {
            const data = await client.memorySearch({
                query,
                scope: scopeSelect.value || '',
                includeArchived: Boolean(archivedCheckbox.checked),
                limit: 20,
                signal: activeAbort?.signal,
            });
            if (seq !== requestSeq) return null;
            setStatus('');
            renderHits(data);
            return data;
        } catch (err) {
            if (err?.name === 'AbortError' || seq !== requestSeq) return null;
            summaryEl.textContent = '';
            if (err?.body?.reason_code === 'index_not_built') {
                setStatus('Memory index is not built yet — it builds automatically when Ouroboros next searches memory.', 'neutral');
            } else {
                setStatus(err?.message || 'Memory search failed.', 'error', { retry: true });
            }
            return null;
        }
    }

    function onSubmit(event) {
        event.preventDefault();
        runSearch(input.value);
    }

    function onScopeOrFilterChange() {
        if (String(input.value || '').trim()) runSearch(input.value);
    }

    function onResultsClick(event) {
        const btn = event.target?.closest?.('[data-copy-hit]');
        if (!btn) return;
        const idx = Number(btn.dataset.copyHit);
        const address = Number.isInteger(idx) ? String(lastHits[idx]?.canonical_address || '') : '';
        if (!address) return;
        Promise.resolve(copyAddress(address)).then((ok) => {
            if (!ok) return;
            btn.textContent = 'Copied';
            const timer = setTimeout(() => {
                copyTimers.delete(timer);
                btn.textContent = 'Copy address';
            }, 1500);
            copyTimers.add(timer);
        }).catch(() => {});
    }

    function onSubtabShown(event) {
        if (event?.detail?.tab === 'memory') {
            refreshProjectScopes();
        }
    }

    syncScopeOptions();
    form.addEventListener('submit', onSubmit);
    scopeSelect.addEventListener('change', onScopeOrFilterChange);
    archivedCheckbox.addEventListener('change', onScopeOrFilterChange);
    resultsEl.addEventListener('click', onResultsClick);
    window.addEventListener('ouro:dashboard-subtab-shown', onSubtabShown);

    return {
        search: runSearch,
        syncScopeOptions,
        refreshProjectScopes,
        destroy() {
            if (activeAbort) activeAbort.abort();
            if (catalogAbort) catalogAbort.abort();
            for (const timer of copyTimers) clearTimeout(timer);
            copyTimers.clear();
            form.removeEventListener('submit', onSubmit);
            scopeSelect.removeEventListener('change', onScopeOrFilterChange);
            archivedCheckbox.removeEventListener('change', onScopeOrFilterChange);
            resultsEl.removeEventListener('click', onResultsClick);
            window.removeEventListener('ouro:dashboard-subtab-shown', onSubtabShown);
        },
    };
}
