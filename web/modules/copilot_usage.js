/** Personal-client Copilot account usage. Credentials never enter the browser.
 * One page-memory store serves all header/detail consumers; /api/state cannot refresh it. */
import { apiFetch } from './api_client.js';

export const USAGE_URL = '/api/extensions/copilot-credits/usage';
const MIN_REFRESH_MS = 60_000;
const REQUEST_MS = 15_000;
const MAX_SNAPSHOT_MS = 24 * 60 * 60_000;
const FRESH_GRACE_MS = 120_000;
const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const dateMs = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
const text = value => typeof value === 'string' && value.trim() ? value : null;

/** Validate the small presentation contract, not arbitrary provider response fields. */
export function usageSnapshot(data, now = Date.now()) {
    if (data?.schema_version !== 1 || !['fresh', 'stale', 'unavailable'].includes(data.status)) throw Error('Unrecognized Copilot usage response');
    if (data.status === 'unavailable') return { ...data, account: null, premium: null, nominal_usd: null, observed_at: null };
    const observed = dateMs(data.observed_at), pct = finite(data.premium?.used_percentage);
    if (!data.premium || observed === null || observed > now + 60_000 ||
        (data.premium.unlimited !== true && (pct === null || pct < 0 || pct > 100))) throw Error('Incomplete Copilot usage response');
    if (now - observed > MAX_SNAPSHOT_MS) return { schema_version: 1, status: 'unavailable', premium: null, account: null, observed_at: null,
        error: { message: 'The last Copilot snapshot is more than 24 hours old.' } };
    return { ...data, status: data.status === 'fresh' && now - observed > FRESH_GRACE_MS ? 'stale' : data.status };
}

export function usagePresentation(snapshot, now = Date.now()) {
    let data;
    try { data = snapshot?.status === 'loading' ? snapshot : usageSnapshot(snapshot, now); }
    catch { data = { status: 'unavailable' }; }
    const premium = data.premium;
    const pct = finite(premium?.used_percentage);
    const available = ['fresh', 'stale'].includes(data.status);
    const label = available ? (premium.unlimited === true ? 'Unlimited' : `${Math.round(pct)}%`) : '—';
    const tone = data.status !== 'fresh' || premium?.unlimited === true ? 'neutral' : pct > 95 ? 'danger' : pct > 90 ? 'warning' : 'normal';
    const observed = dateMs(data.observed_at);
    const when = observed === null ? '' : new Date(observed).toLocaleString();
    const description = data.status === 'loading' ? 'Checking GitHub Copilot account usage' :
        data.status === 'unavailable' ? 'GitHub Copilot account usage unavailable' :
        `${data.status === 'stale' ? 'Last known ' : ''}${label}${premium.unlimited === true ? '' : ` used (provider ${pct}%)`} across GitHub Copilot apps${when ? `; checked ${when}` : ''}${data.status === 'stale' ? '; stale' : ''}`;
    return { data, label, tone, fill: available && premium.unlimited !== true ? pct : null,
        stale: data.status === 'stale', description };
}

/** Injectable transport/clock for deterministic cache, abort and freshness tests. */
export function createUsageStore({ fetchImpl = apiFetch, now = () => Date.now(), doc = globalThis.document,
    setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    let current = { status: 'loading' }, timer = null, flight = null, lastAttempt = -Infinity, nextAttempt = 0;
    const subscribers = new Set();
    const notify = () => { for (const callback of [...subscribers]) callback(current); };
    const visible = () => doc?.visibilityState !== 'hidden';
    function schedule() {
        if (timer !== null) clearTimer(timer);
        timer = null;
        if (!subscribers.size || !visible()) return;
        timer = setTimer(() => { timer = null; void refresh(); }, Math.max(0, nextAttempt - now()));
    }
    async function refresh() {
        if (!subscribers.size || !visible() || flight) return;
        if (now() < nextAttempt) { schedule(); return; }
        lastAttempt = now();
        nextAttempt = lastAttempt + MIN_REFRESH_MS;
        const request = { controller: new AbortController(), timeout: null };
        flight = request;
        request.timeout = setTimer(() => request.controller.abort(), REQUEST_MS);
        try {
            const response = await fetchImpl(USAGE_URL, { cache: 'no-store', signal: request.controller.signal });
            if (flight !== request || !subscribers.size) return;
            if (!response.ok) {
                current = { status: 'unavailable' }; // The host did not deliver this extension's usage contract.
                throw Error(`Copilot extension unavailable (HTTP ${response.status})`);
            }
            const data = usageSnapshot(await response.json(), now());
            if (flight !== request || !subscribers.size) return;
            current = data;
            const serverNext = dateMs(data.next_refresh_after);
            // Honor server backoff but never make a browser failure a permanent stale cache.
            if (serverNext !== null) nextAttempt = Math.max(nextAttempt, Math.min(now() + 15 * MIN_REFRESH_MS, serverNext));
        } catch (error) {
            if (flight !== request || !subscribers.size) return;
            const old = usagePresentation(current, now()).data;
            current = ['fresh', 'stale'].includes(old.status) ? { ...old, status: 'stale',
                error: { message: 'Could not refresh Copilot usage. Showing the last checked snapshot.' } } :
                { schema_version: 1, status: 'unavailable', account: null, premium: null, observed_at: null,
                    error: { message: 'Copilot usage is unavailable. The extension may be offline or not configured.' } };
            current.last_attempt_at = new Date(lastAttempt).toISOString();
            current.next_refresh_after = new Date(nextAttempt).toISOString();
        } finally {
            clearTimer(request.timeout);
            if (flight === request) { flight = null; notify(); schedule(); }
        }
    }
    function onVisibility() { if (visible()) { notify(); void refresh(); } else schedule(); }
    return {
        subscribe(callback) {
            subscribers.add(callback);
            callback(current);
            if (subscribers.size === 1) doc?.addEventListener('visibilitychange', onVisibility);
            void refresh();
            let released = false;
            return () => {
                if (released) return; released = true;
                subscribers.delete(callback);
                if (subscribers.size) return;
                doc?.removeEventListener('visibilitychange', onVisibility);
                if (timer !== null) clearTimer(timer); timer = null;
                if (flight) { const request = flight; flight = null; clearTimer(request.timeout); request.controller.abort(); }
            };
        },
        // Refresh remains bounded, including repeated button clicks and navigation.
        refresh,
        peek: () => current,
    };
}

export const copilotUsage = createUsageStore();

export function bindCopilotHeader(pill, store = copilotUsage) {
    if (!pill) return () => {};
    pill.classList.add('copilot-usage-pill');
    return store.subscribe(snapshot => {
        const view = usagePresentation(snapshot);
        pill.dataset.usageTone = view.tone;
        pill.dataset.usageStatus = view.data.status;
        pill.querySelector('.chat-budget-text').textContent = view.label;
        pill.querySelector('.copilot-usage-stale').hidden = !view.stale;
        pill.style.setProperty('--budget-fill', `${view.fill ?? 0}%`);
        const bar = pill.querySelector('.chat-budget-bar');
        if (view.fill === null) { bar.removeAttribute('aria-valuenow'); bar.setAttribute('aria-valuetext', view.description); }
        else { bar.setAttribute('aria-valuenow', String(view.fill)); bar.removeAttribute('aria-valuetext'); }
        pill.title = `${view.description}. Open Costs`;
        pill.setAttribute('aria-label', `${view.description}. Open Costs`);
    });
}

export function copilotDetailsHtml() {
    return `<section class="copilot-credits-card" aria-labelledby="copilot-credits-title">
        <div class="copilot-credits-head"><h3 id="copilot-credits-title">GitHub Copilot</h3>
        <span class="copilot-credits-auto">Updates automatically</span></div>
        <div class="copilot-credits-primary"><strong data-copilot-field="percentage">—</strong><span data-copilot-field="status">Checking…</span></div>
        <p class="copilot-credits-scope">Account total across Copilot apps, not Viper-only usage.</p>
        <dl class="copilot-credits-grid">
            <div><dt>Account</dt><dd data-copilot-field="account">—</dd></div>
            <div><dt>Plan</dt><dd data-copilot-field="plan">—</dd></div>
            <div><dt>Used (provider percentage)</dt><dd data-copilot-field="usedPercentage">—</dd></div>
            <div><dt>Remaining (provider percentage)</dt><dd data-copilot-field="remainingPercentage">—</dd></div>
            <div><dt>Allowance (credits)</dt><dd data-copilot-field="entitlement">—</dd></div>
            <div><dt>Remaining (credits)</dt><dd data-copilot-field="remaining">—</dd></div>
            <div><dt>Credits used (provider counter)</dt><dd data-copilot-field="used">—</dd></div>
            <div><dt>Resets</dt><dd data-copilot-field="reset">—</dd></div>
            <div><dt>Allowance consumption value</dt><dd data-copilot-field="nominalEntitlement">—</dd></div>
            <div><dt>Remaining consumption value</dt><dd data-copilot-field="nominalRemaining">—</dd></div>
        </dl>
        <p class="copilot-credits-note">Percentage and credits-used are separate provider counters and may not reconcile. Consumption value uses 1 credit = $0.01 USD; it is not an invoice.</p>
        <div class="copilot-credits-meta"><span data-copilot-field="checked">Not checked</span><span data-copilot-field="attempt"></span><span data-copilot-field="next"></span><span data-copilot-field="source"></span></div>
        <p class="copilot-credits-error" data-copilot-field="error" role="status" hidden></p>
    </section>`;
}

export function bindCopilotCosts(page, { state, store = copilotUsage, win = window } = {}) {
    const section = page.querySelector('.copilot-credits-card');
    let release = null, destroyed = false;
    const fields = Object.fromEntries([...section.querySelectorAll('[data-copilot-field]')].map(e => [e.dataset.copilotField, e]));
    const number = value => finite(value) === null ? 'Unknown' : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
    const date = value => dateMs(value) === null ? 'Unknown' : new Date(value).toLocaleString(undefined, { timeZoneName: 'short' });
    function render(snapshot) {
        const view = usagePresentation(snapshot), data = view.data, p = data.premium;
        const available = ['fresh', 'stale'].includes(data.status);
        section.dataset.usageTone = view.tone;
        fields.percentage.textContent = available ? view.label : '—';
        fields.status.textContent = data.status === 'loading' ? 'Checking…' : data.status === 'unavailable' ? 'Unavailable' : data.status === 'stale' ? 'Last known usage · stale' : p.unlimited ? 'Unlimited allowance' : 'used';
        fields.account.textContent = text(data.account?.login) || 'Unknown';
        fields.plan.textContent = text(data.account?.plan) || 'Unknown';
        fields.usedPercentage.textContent = finite(p?.used_percentage) === null ? 'Unknown' : `${number(p.used_percentage)}%`;
        fields.remainingPercentage.textContent = finite(p?.percent_remaining) === null ? 'Unknown' : `${number(p.percent_remaining)}%`;
        fields.entitlement.textContent = p?.unlimited ? 'Unlimited' : number(p?.entitlement);
        fields.remaining.textContent = p?.unlimited ? 'Unlimited' : number(p?.quota_remaining);
        fields.used.textContent = number(p?.credits_used);
        fields.reset.textContent = date(data.reset_at);
        const nominal = data.nominal_usd?.basis === '1 credit = $0.01 consumption value; not an invoice' ? data.nominal_usd : null;
        const usd = value => finite(value) === null ? 'Unknown' : value.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
        fields.nominalEntitlement.textContent = usd(nominal?.entitlement);
        fields.nominalRemaining.textContent = usd(nominal?.quota_remaining);
        fields.checked.textContent = data.observed_at ? `Checked ${date(data.observed_at)}` : 'No successful snapshot';
        fields.attempt.textContent = data.last_attempt_at ? `Last attempt ${date(data.last_attempt_at)}` : '';
        fields.next.textContent = data.next_refresh_after ? `Next refresh after ${date(data.next_refresh_after)}` : '';
        fields.source.textContent = data.source ? `Source: ${data.source}` : '';
        fields.error.textContent = text(data.error?.message) || (data.status === 'stale' ? 'This snapshot is stale. Refresh will retry automatically.' : '');
        fields.error.hidden = !fields.error.textContent;
    }
    function sync() {
        const shown = state.activePage === 'dashboard' && state.dashboardActiveSubtab === 'costs';
        if (shown && !release && !destroyed) release = store.subscribe(render);
        else if (!shown && release) { release(); release = null; }
    }
    win.addEventListener('ouro:dashboard-subtab-shown', sync);
    win.addEventListener('ouro:page-shown', sync);
    function destroy(event) {
        if (event?.persisted) return; // browser owns freezing/restoring a retained page
        destroyed = true;
        release?.(); release = null;
        win.removeEventListener('ouro:dashboard-subtab-shown', sync);
        win.removeEventListener('ouro:page-shown', sync);
        win.removeEventListener('pagehide', destroy);
    }
    win.addEventListener('pagehide', destroy);
    sync();
    return destroy;
}
