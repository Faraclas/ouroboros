import { copilotDetailsHtml, bindCopilotCosts, copilotUsage, HISTORY_URL } from './copilot_usage.js';
import { formatUsd2 } from './utils.js';
import { apiFetch } from './api_client.js';

const COST_BUDGET_INPUTS = {
    TOTAL_BUDGET: 's-budget',
    OUROBOROS_PER_TASK_COST_USD: 's-per-task-cost',
};

function readPositiveBudget(id) {
    const input = document.getElementById(id);
    const raw = String(input?.value || '').trim();
    const value = Number(raw);
    const min = Number(input?.min || 0.01);
    return Number.isFinite(value) && value >= min ? value : null;
}

function optionalFiniteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

/** Render one legacy breakdown bucket without turning an open zero into free. */
export function costBucketPresentation(info) {
    const cost = optionalFiniteNumber(info?.cost);
    if (cost === null) return 'unknown';
    const unknown = optionalFiniteNumber(info?.unknown_unmetered) || 0;
    const nonFinalRows = optionalFiniteNumber(info?.non_final_rows) || 0;
    const pending = info?.cost_final === false || nonFinalRows > 0 || unknown > 0;
    if (!pending) return formatUsd2(cost);
    const unknownLabel = unknown > 0 ? `unmetered=${Math.trunc(unknown)}` : '';
    const details = [unknownLabel].filter(Boolean).join(', ');
    if (cost === 0) return details ? `cost pending (${details})` : 'cost pending';
    return details ? `${formatUsd2(cost)} (pending, ${details})` : `${formatUsd2(cost)} (pending)`;
}

// A plain "saved" is an acknowledgement of what the owner just did and ends by itself; anything the owner may still
// need to act on or read (a restart still required, a warning, an error) stays until the next save replaces it.
const SAVE_ACK_MS = 4000;

/** What one budget save says and whether that saying may end by itself: `{ text, tone, transient }`. */
export function budgetSavePresentation(data = {}) {
    let text;
    // `restart_required` is the server's whole restart state, so with nothing changed it is a setting saved earlier.
    if (data.no_changes) text = data.restart_required ? 'No changes. Restart still required.' : 'No changes.';
    else if (data.restart_required) text = 'Saved. Restart required.';
    else if (data.immediate_changed && data.next_task_changed) {
        text = 'Saved. Some changes took effect immediately; others apply on the next task.';
    } else if (data.immediate_changed) text = 'Saved. Took effect immediately.';
    else text = 'Saved. Applies on the next task.';
    const warned = Boolean(data.warnings?.length);
    if (warned) text += ' Warnings: ' + data.warnings.join(' | ');
    const standing = warned || Boolean(data.restart_required);
    return { text, tone: standing ? 'warn' : 'ok', transient: !standing };
}

/** Extension callbacks the ledger recorded, apart from AI model requests; never a priced zero. */
export function extensionActivityPresentation(data) {
    const activity = data?.extension_activity;
    if (!activity || typeof activity !== 'object') return { state: 'unavailable', rows: [], total: '—', note: 'Unavailable' };
    const rows = Object.entries(activity.by_extension || {}).map(([name, info]) => ({
        name,
        calls: String(Math.trunc(optionalFiniteNumber(info?.calls) || 0)),
        surfaces: Object.entries(info?.surfaces || {})
            .map(([surface, count]) => `${surface}: ${Math.trunc(optionalFiniteNumber(count) || 0)}`).join(', ') || '-',
        cost: 'Not metered',
    }));
    return {
        state: rows.length ? 'available' : 'none',
        rows,
        total: String(Math.trunc(optionalFiniteNumber(activity.calls) || 0)),
        note: String(activity.cost_note || ''),
    };
}

/** Pure cost-dashboard projection: null/unavailable never renders as $0.
 * The limit is set against KNOWN spend (settled: confirmed + estimated), the number
 * that refuses new paid calls (#1487); reservations and unresolved upper bounds are
 * shown beside it as open holds, never added in. */
export function costDashboardPresentation(data) {
    if (!data) return { state: 'loading' };
    const accounting = data.accounting || {};
    if (accounting.available === false) return { state: 'unavailable' };
    const known = optionalFiniteNumber(accounting.settled_usd);
    const confirmed = optionalFiniteNumber(accounting.confirmed_usd);
    const estimated = optionalFiniteNumber(accounting.estimated_usd);
    const reserved = optionalFiniteNumber(accounting.reserved_usd);
    const unresolved = optionalFiniteNumber(accounting.unresolved_upper_bound_usd);
    const unknown = optionalFiniteNumber(accounting.unknown_unmetered);
    const calls = optionalFiniteNumber(data.total_calls);
    if ([known, confirmed, estimated, reserved, unresolved, unknown, calls].some(value => value === null)) {
        return { state: 'unavailable' };
    }
    const rawLimit = optionalFiniteNumber(accounting.limit_usd);
    const limit = rawLimit !== null && rawLimit > 0 ? rawLimit : 0;
    const models = Object.entries(data.by_model || {});
    // A flag without its cause is not reconstructible. `cost_final: false` holds with every
    // dollar bucket at $0.00 and `unknown` at 0 — an ESTIMATED zero, or a dispatched row
    // whose reservation is exactly zero — so the count of open rows rides beside the flag
    // it explains rather than in a new tile nobody correlates. Absent on an older payload,
    // and then this says only what it knows instead of inventing a zero.
    const nonFinal = optionalFiniteNumber(accounting.non_final_rows);
    const openCause = nonFinal !== null && nonFinal > 0 ? ` (${Math.trunc(nonFinal)} open)` : '';
    return {
        state: 'available',
        knownLimit: `${formatUsd2(known)} / ${limit > 0 ? formatUsd2(limit) : '∞'}`,
        confirmed: formatUsd2(confirmed),
        estimated: formatUsd2(estimated),
        reserved: formatUsd2(reserved),
        unresolved: formatUsd2(unresolved),
        unknown: String(Math.trunc(unknown)),
        final: accounting.cost_final === true ? 'Yes' : `Pending${openCause}`,
        calls: String(Math.trunc(calls)),
        extensionCalls: extensionActivityPresentation(data).total,
        topModel: models.length > 0 ? models[0][0] : '-',
    };
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH_RE = /^\d{4}-\d{2}$/;
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const FULL_MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];
const CHART_W = 760;
const CHART_H = 276;
const CHART_PAD = { top: 18, right: 20, bottom: 34, left: 58 };

function round6(value) {
    return Math.round(value * 1e6) / 1e6;
}

function nyDateFromMs(ms) {
    try {
        const parts = new Intl.DateTimeFormat('en-CA', {
            timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
        }).formatToParts(new Date(ms));
        const map = Object.fromEntries(parts.map(p => [p.type, p.value]));
        if (map.year && map.month && map.day) return `${map.year}-${map.month}-${map.day}`;
    } catch {}
    return new Date(ms).toISOString().slice(0, 10);
}

function formatMonthLabel(month) {
    if (!MONTH_RE.test(String(month || ''))) return String(month || '');
    const [year, m] = month.split('-');
    const idx = Number(m) - 1;
    return idx >= 0 && idx < 12 ? `${FULL_MONTH_NAMES[idx]} ${year}` : month;
}

function formatShortDate(isoDate) {
    if (!DATE_RE.test(String(isoDate || ''))) return String(isoDate || '');
    const [, m, d] = isoDate.split('-');
    const idx = Number(m) - 1;
    return idx >= 0 && idx < 12 ? `${MONTH_NAMES[idx]} ${Number(d)}` : isoDate;
}

function addUtcDay(isoDate) {
    const [y, m, d] = isoDate.split('-').map(Number);
    const next = new Date(Date.UTC(y, m - 1, d + 1));
    return next.toISOString().slice(0, 10);
}

function formatCredits(value, compact = false) {
    if (value === null || value === undefined || !Number.isFinite(value)) return '—';
    if (compact) {
        if (Math.abs(value) >= 1000) return `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k`;
        return value.toLocaleString(undefined, { maximumFractionDigits: value < 10 ? 1 : 0 });
    }
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} cr`;
}

function formatSpendUsd(value, compact = false) {
    if (value === null || value === undefined || !Number.isFinite(value)) return '—';
    if (compact) {
        if (Math.abs(value) >= 1000) return `$${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k`;
        return `$${value.toLocaleString(undefined, { maximumFractionDigits: value < 10 ? 2 : 0 })}`;
    }
    return value.toLocaleString(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatSpendValue(value, unit = 'credits', { compact = false } = {}) {
    return unit === 'usd' ? formatSpendUsd(value, compact) : formatCredits(value, compact);
}

function niceCeiling(maxVal) {
    if (!Number.isFinite(maxVal) || maxVal <= 0) return 1;
    const rawStep = maxVal / 4;
    const mag = 10 ** Math.floor(Math.log10(rawStep));
    const norm = rawStep / mag;
    const niceNorm = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return niceNorm * mag * 4;
}

/** Pure monthly spend merger: strictly provider-reported Viper credits + recorded daily Copilot snapshots.
 * Never estimates missing Viper credits from tokens and never invents historical Copilot snapshots. */
export function monthlySpendSeries(dailySpend, copilotHistory = null, liveSnapshot = null, nowMs = Date.now()) {
    const viperAvailable = dailySpend?.available === true;
    const historyOk = copilotHistory?.status === 'ok' && Array.isArray(copilotHistory.days);
    const today = DATE_RE.test(String(dailySpend?.today || '')) ? dailySpend.today
        : DATE_RE.test(String(copilotHistory?.today || '')) ? copilotHistory.today : nyDateFromMs(nowMs);
    const month = MONTH_RE.test(String(dailySpend?.month || '')) ? dailySpend.month
        : MONTH_RE.test(String(copilotHistory?.month || '')) ? copilotHistory.month : today.slice(0, 7);
    const creditUsd = optionalFiniteNumber(dailySpend?.credit_usd)
        ?? optionalFiniteNumber(copilotHistory?.credit_usd) ?? 0.01;
    const prefix = month + '-';

    const viperByDay = new Map();
    if (viperAvailable && Array.isArray(dailySpend.days)) {
        for (const row of dailySpend.days) {
            const date = String(row?.date || '');
            const cum = optionalFiniteNumber(row?.viper_cumulative_credits);
            if (!DATE_RE.test(date) || !date.startsWith(prefix) || date > today || cum === null || cum < 0) continue;
            viperByDay.set(date, {
                cumulativeCredits: cum,
                dailyCredits: optionalFiniteNumber(row?.viper_daily_credits) ?? 0,
                calls: Math.max(0, Math.trunc(optionalFiniteNumber(row?.viper_calls) ?? 0)),
            });
        }
    }

    const totalByDay = new Map();
    if (historyOk) {
        for (const row of copilotHistory.days) {
            const date = String(row?.date || '');
            const used = optionalFiniteNumber(row?.credits_used);
            if (!DATE_RE.test(date) || !date.startsWith(prefix) || date > today || used === null || used < 0) continue;
            totalByDay.set(date, {
                creditsUsed: used,
                entitlement: optionalFiniteNumber(row?.entitlement),
                observedAt: typeof row?.observed_at === 'string' ? row.observed_at : null,
            });
        }
    }

    let hasLiveSnapshot = false;
    if (liveSnapshot?.status === 'fresh') {
        const used = optionalFiniteNumber(liveSnapshot.premium?.credits_used);
        const observedMs = typeof liveSnapshot.observed_at === 'string' ? Date.parse(liveSnapshot.observed_at) : NaN;
        if (used !== null && used >= 0 && Number.isFinite(observedMs)) {
            const liveDate = nyDateFromMs(observedMs);
            if (liveDate.startsWith(prefix) && liveDate <= today) {
                const existing = totalByDay.get(liveDate);
                const existingMs = existing?.observedAt ? Date.parse(existing.observedAt) : -Infinity;
                if (!existing || !Number.isFinite(existingMs) || observedMs >= existingMs) {
                    totalByDay.set(liveDate, {
                        creditsUsed: used,
                        entitlement: optionalFiniteNumber(liveSnapshot.premium?.entitlement),
                        observedAt: liveSnapshot.observed_at,
                    });
                    hasLiveSnapshot = true;
                }
            }
        }
    }

    const historyAvailable = historyOk || hasLiveSnapshot;
    const allDates = [...new Set([...viperByDay.keys(), ...totalByDay.keys()])].sort();
    if (!allDates.length) {
        return {
            state: viperAvailable || historyAvailable ? 'empty' : 'unavailable',
            month, monthLabel: formatMonthLabel(month), today, creditUsd,
            viperAvailable, historyAvailable, days: [],
            latest: { viper: null, total: null, others: null },
        };
    }

    const firstDate = allDates[0];
    const endDate = allDates[allDates.length - 1] > today ? allDates[allDates.length - 1] : today;
    const days = [];
    let cursor = firstDate;
    let lastViper = null;
    while (cursor <= endDate && days.length < 32) {
        const v = viperByDay.get(cursor);
        if (v) lastViper = v.cumulativeCredits;
        const viperCredits = lastViper;
        const t = totalByDay.get(cursor);
        const totalCredits = t ? t.creditsUsed : null;
        const othersCredits = (totalCredits !== null && viperAvailable)
            ? Math.max(0, round6(totalCredits - (viperCredits ?? 0))) : null;
        days.push({
            date: cursor,
            label: formatShortDate(cursor),
            viperCredits,
            viperDailyCredits: v ? v.dailyCredits : (viperCredits !== null ? 0 : null),
            viperCalls: v ? v.calls : 0,
            totalCredits,
            othersCredits,
            viperUsd: viperCredits === null ? null : round6(viperCredits * creditUsd),
            totalUsd: totalCredits === null ? null : round6(totalCredits * creditUsd),
            othersUsd: othersCredits === null ? null : round6(othersCredits * creditUsd),
        });
        cursor = addUtcDay(cursor);
    }

    const pickLatest = (creditKey, usdKey) => {
        for (let i = days.length - 1; i >= 0; i -= 1) {
            if (days[i][creditKey] !== null) {
                return { date: days[i].date, label: days[i].label, credits: days[i][creditKey], usd: days[i][usdKey] };
            }
        }
        return null;
    };
    return {
        state: 'available',
        month, monthLabel: formatMonthLabel(month), today, creditUsd,
        viperAvailable, historyAvailable, days,
        latest: {
            viper: pickLatest('viperCredits', 'viperUsd'),
            total: pickLatest('totalCredits', 'totalUsd'),
            others: pickLatest('othersCredits', 'othersUsd'),
        },
    };
}

function seriesPaths(points) {
    const segments = [];
    let run = [];
    for (const pt of points) {
        if (pt === null) {
            if (run.length) { segments.push(run); run = []; }
        } else {
            run.push(pt);
        }
    }
    if (run.length) segments.push(run);
    return segments.filter(seg => seg.length >= 2)
        .map(seg => seg.map((p, idx) => `${idx === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '))
        .join(' ');
}

/** Build responsive SVG markup (no inline style attributes) for the 3-line monthly spend chart. */
export function monthlySpendSvg(model, unit = 'credits', selectedIndex = -1, { width = CHART_W } = {}) {
    const days = model?.days || [];
    const chartW = Math.max(300, Math.min(1100, Math.round(Number(width) || CHART_W)));
    const narrow = chartW < 480;
    const chartH = narrow ? 224 : CHART_H;
    const pad = narrow
        ? { top: 16, right: 14, bottom: 30, left: 48 }
        : CHART_PAD;
    if (!days.length) return { svgHtml: '', ariaLabel: 'No monthly spend data available', yMax: 0, width: chartW, height: chartH };
    const isUsd = unit === 'usd';
    const valueOf = (day, key) => isUsd ? day[`${key}Usd`] : day[`${key}Credits`];
    let maxVal = 0;
    for (const day of days) {
        for (const key of ['viper', 'total', 'others']) {
            const v = valueOf(day, key);
            if (v !== null && v > maxVal) maxVal = v;
        }
    }
    const yMax = niceCeiling(maxVal);
    const plotW = chartW - pad.left - pad.right;
    const plotH = chartH - pad.top - pad.bottom;
    const xFor = (idx) => days.length === 1
        ? pad.left + plotW / 2
        : pad.left + (idx / (days.length - 1)) * plotW;
    const yFor = (val) => pad.top + plotH - (val / yMax) * plotH;

    const parts = [];
    for (let step = 0; step <= 4; step += 1) {
        const tickVal = (yMax * step) / 4;
        const y = yFor(tickVal);
        parts.push(`<line class="costs-spend-grid-line" x1="${pad.left}" y1="${y.toFixed(1)}" x2="${chartW - pad.right}" y2="${y.toFixed(1)}"/>`);
        parts.push(`<text class="costs-spend-axis-label costs-spend-y-label" x="${pad.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${formatSpendValue(tickVal, unit, { compact: true })}</text>`);
    }

    const maxTicks = narrow ? 4 : 7;
    const tickIndices = new Set([0, days.length - 1]);
    if (days.length > 2) {
        const stride = Math.max(1, Math.ceil((days.length - 1) / (maxTicks - 1)));
        for (let i = stride; i < days.length - 1; i += stride) {
            if (days.length - 1 - i >= Math.ceil(stride / 2)) tickIndices.add(i);
        }
    }
    for (const idx of [...tickIndices].sort((a, b) => a - b)) {
        const x = xFor(idx);
        const anchor = days.length > 1 && idx === 0 ? 'start' : days.length > 1 && idx === days.length - 1 ? 'end' : 'middle';
        parts.push(`<text class="costs-spend-axis-label costs-spend-x-label" x="${x.toFixed(1)}" y="${chartH - 8}" text-anchor="${anchor}">${days[idx].label}</text>`);
    }

    if (selectedIndex >= 0 && selectedIndex < days.length) {
        const sx = xFor(selectedIndex).toFixed(1);
        parts.push(`<line class="costs-spend-guide" x1="${sx}" y1="${pad.top}" x2="${sx}" y2="${pad.top + plotH}"/>`);
    }

    const seriesOrder = ['total', 'others', 'viper'];
    for (const key of seriesOrder) {
        const pts = days.map((day, idx) => {
            const v = valueOf(day, key);
            return v === null ? null : { x: xFor(idx), y: yFor(v), idx, v };
        });
        const d = seriesPaths(pts);
        if (d) {
            parts.push(`<path class="costs-spend-line costs-spend-line--${key}" d="${d}"/>`);
        }
        for (let i = 0; i < pts.length; i += 1) {
            const pt = pts[i];
            if (!pt) continue;
            const isolated = (i === 0 || !pts[i - 1]) && (i === pts.length - 1 || !pts[i + 1]);
            const isSelected = i === selectedIndex;
            if (isolated || isSelected) {
                parts.push(`<circle class="costs-spend-halo costs-spend-halo--${key}" cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="${isSelected ? 7.5 : 6.5}"/>`);
            }
            parts.push(`<circle class="costs-spend-dot costs-spend-dot--${key}" cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="${isolated || isSelected ? 4.2 : 3}"/>`);
        }
    }

    const slotW = days.length === 1 ? plotW : plotW / (days.length - 1);
    for (let i = 0; i < days.length; i += 1) {
        const cx = xFor(i);
        const rx = Math.max(pad.left, cx - slotW / 2);
        const rw = Math.min(chartW - pad.right, cx + slotW / 2) - rx;
        parts.push(`<rect class="costs-spend-hit" data-day-index="${i}" x="${rx.toFixed(1)}" y="${pad.top}" width="${Math.max(8, rw).toFixed(1)}" height="${plotH}" tabindex="0" role="button" aria-label="${days[i].label}: Viper ${formatSpendValue(valueOf(days[i], 'viper'), unit)}, Total GitHub Copilot ${formatSpendValue(valueOf(days[i], 'total'), unit)}, Everyone else ${formatSpendValue(valueOf(days[i], 'others'), unit)}"/>`);
    }

    const unitLabel = isUsd ? 'consumption USD' : 'Copilot credits';
    const ariaLabel = `Daily cumulative monthly spend for ${model.monthLabel} in ${unitLabel}, ${days[0].label} to ${days[days.length - 1].label}`;
    return { svgHtml: parts.join(''), ariaLabel, yMax, width: chartW, height: chartH };
}

export function initCosts({ state, mount }) {
    const page = document.createElement('div');
    page.id = 'page-costs';
    page.className = 'settings-embedded-content settings-costs-panel';
    page.innerHTML = `
        <div class="costs-scroll">
            ${copilotDetailsHtml()}
            <h3 class="copilot-app-accounting">Viper call accounting</h3>
            <details class="copilot-app-budget"><summary>App safety budget</summary>
            <p class="copilot-app-budget-note">These application dispatch limits are separate from Copilot account credits.</p>
            <div class="costs-budget-card">
                <div class="costs-budget-head">
                    <h3 class="costs-budget-title">Application limits</h3>
                    <button class="btn btn-default btn-sm costs-budget-refresh" id="btn-refresh-costs">Refresh</button>
                </div>
                <div class="costs-budget-fields">
                    <div class="form-field ui-field">
                        <label for="s-budget">Total Budget ($)</label>
                        <input id="s-budget" class="ui-control" type="number" value="200">
                    </div>
                    <div class="form-field ui-field">
                        <label for="s-per-task-cost">Per-task Cost Cap ($)</label>
                        <input id="s-per-task-cost" class="ui-control" type="number" value="50" aria-describedby="costs-per-task-note">
                        <div class="settings-inline-note ui-field-help" id="costs-per-task-note">Hard dispatch cap for the whole root task tree, reached when its known spend (confirmed and estimated) gets there. In-flight calls settle normally; increasing the cap does not auto-resume paused work.</div>
                    </div>
                </div>
                <button class="btn btn-save costs-budget-save" id="btn-save-budget">Save Budget</button>
                <div id="budget-save-status" class="settings-inline-status"></div>
            </div>
            </details>
            <div class="costs-stats-grid">
                <div class="stat-card"><div class="label">Known spend / Limit</div><div class="value" id="cost-known-limit">Loading…</div></div>
                <div class="stat-card"><div class="label">Confirmed</div><div class="value" id="cost-confirmed">—</div></div>
                <div class="stat-card"><div class="label">Estimated</div><div class="value" id="cost-estimated">—</div></div>
                <div class="stat-card"><div class="label">Reserved</div><div class="value" id="cost-reserved">—</div></div>
                <div class="stat-card"><div class="label">Unresolved upper bound</div><div class="value" id="cost-unresolved">—</div></div>
                <div class="stat-card"><div class="label">Unknown / unmetered</div><div class="value" id="cost-unknown">—</div></div>
                <div class="stat-card"><div class="label">Cost final</div><div class="value" id="cost-final">Loading…</div></div>
                <div class="stat-card"><div class="label">Model requests / unattributed</div><div class="value" id="cost-calls">—</div></div>
                <div class="stat-card"><div class="label">Extension callbacks (not metered)</div><div class="value" id="cost-extension-calls">—</div></div>
                <div class="stat-card"><div class="label">Top Model</div><div class="value cost-top-model" id="cost-top-model">-</div></div>
            </div>
            <div class="costs-tables-grid">
                <div>
                    <h3 class="costs-table-label">By Model</h3>
                    <table class="cost-table" id="cost-by-model"><thead><tr><th>Model</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By API Key</h3>
                    <table class="cost-table" id="cost-by-key"><thead><tr><th>Key</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Model Category</h3>
                    <table class="cost-table" id="cost-by-model-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Task Category</h3>
                    <table class="cost-table" id="cost-by-task-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
            <div class="costs-extension-activity" id="cost-extension-activity">
                <p class="copilot-app-budget-note">Request tables exclude proven extension callbacks. Unproven dispatches remain unattributed; unknown cost and finality above cover the whole ledger.</p>
                <h3 class="costs-table-label">Extension activity</h3>
                <p class="copilot-app-budget-note" id="cost-extension-note">Add-ons running their own callbacks, counted apart from AI model requests.</p>
                <table class="cost-table" id="cost-by-extension"><thead><tr><th>Extension</th><th>Callbacks</th><th>Kinds</th></tr></thead><tbody></tbody></table>
            </div>
            </div>
            <section class="costs-monthly-spend-card" id="cost-monthly-spend" aria-labelledby="cost-monthly-spend-title">
                <div class="costs-spend-head">
                    <div class="costs-spend-title-wrap">
                        <h3 class="costs-spend-title" id="cost-monthly-spend-title">Daily cumulative monthly spend</h3>
                        <span class="costs-spend-period" id="cost-spend-period"></span>
                    </div>
                    <div class="costs-spend-toggle" role="group" aria-label="Monthly spend unit">
                        <button type="button" class="costs-spend-unit-btn is-active" id="cost-spend-unit-credits" data-spend-unit="credits" aria-pressed="true">Copilot credits</button>
                        <button type="button" class="costs-spend-unit-btn" id="cost-spend-unit-usd" data-spend-unit="usd" aria-pressed="false">USD ($)</button>
                    </div>
                </div>
                <div class="costs-spend-legend" id="cost-spend-legend">
                    <div class="costs-spend-legend-item">
                        <span class="costs-spend-swatch costs-spend-swatch--viper" aria-hidden="true"></span>
                        <span class="costs-spend-legend-name">Viper</span>
                        <strong class="costs-spend-legend-val" id="cost-spend-val-viper">—</strong>
                    </div>
                    <div class="costs-spend-legend-item">
                        <span class="costs-spend-swatch costs-spend-swatch--total" aria-hidden="true"></span>
                        <span class="costs-spend-legend-name">Total GitHub Copilot</span>
                        <strong class="costs-spend-legend-val" id="cost-spend-val-total">—</strong>
                    </div>
                    <div class="costs-spend-legend-item">
                        <span class="costs-spend-swatch costs-spend-swatch--others" aria-hidden="true"></span>
                        <span class="costs-spend-legend-name">Everyone else</span>
                        <strong class="costs-spend-legend-val" id="cost-spend-val-others">—</strong>
                    </div>
                </div>
                <div class="costs-spend-readout" id="cost-spend-readout" aria-live="polite"></div>
                <div class="costs-spend-canvas" id="cost-spend-canvas">
                    <svg class="costs-spend-svg" id="cost-spend-svg" viewBox="0 0 760 276" role="img" aria-label="Daily cumulative monthly spend chart"></svg>
                    <p class="costs-spend-empty" id="cost-spend-empty" hidden>No monthly spend data recorded yet.</p>
                </div>
                <details class="costs-spend-table-disclosure" id="cost-spend-table-disclosure">
                    <summary class="costs-spend-table-summary" id="cost-spend-table-summary">Daily cumulative breakdown table</summary>
                    <div class="costs-spend-table-wrap">
                        <table class="cost-table costs-spend-table" id="cost-monthly-spend-table">
                            <caption class="costs-spend-sr-only" id="cost-spend-table-caption">Daily cumulative monthly spend</caption>
                            <thead>
                                <tr>
                                    <th scope="col">Date (EST/EDT)</th>
                                    <th scope="col" class="cost-cell-right" id="cost-spend-th-viper">Viper (credits)</th>
                                    <th scope="col" class="cost-cell-right" id="cost-spend-th-total">Total GitHub Copilot (credits)</th>
                                    <th scope="col" class="cost-cell-right" id="cost-spend-th-others">Everyone else (credits)</th>
                                </tr>
                            </thead>
                            <tbody></tbody>
                        </table>
                    </div>
                </details>
                <p class="costs-spend-note" id="cost-spend-note">
                    Day boundaries follow 12:00 AM America/New_York (EST/EDT). Viper reflects strictly provider-reported <code>copilot_usage.total_nano_aiu</code> (missing streaming or historical credits are not estimated). Total GitHub Copilot tracks daily <code>credits_used</code> snapshots forward from when snapshots begin (no invented history); Everyone else is Total minus Viper on days with a snapshot. Provider percentage and <code>credits_used</code> are separate counters and consumption USD ($0.01/credit) is for visual trend and share comparison, not penny-accurate invoice reconciliation.
                </p>
            </section>
        </div>
    `;
    mount.appendChild(page);

    let spendUnit = 'credits';
    let latestDailySpend = null;
    let latestCopilotHistory = null;
    let latestLiveCopilot = copilotUsage.peek();
    let selectedDayIdx = -1;
    let currentSpendModel = null;

    function renderMonthlySpend() {
        const model = monthlySpendSeries(latestDailySpend, latestCopilotHistory, latestLiveCopilot);
        currentSpendModel = model;
        const isUsd = spendUnit === 'usd';
        const unitShort = isUsd ? 'USD' : 'credits';
        const periodEl = document.getElementById('cost-spend-period');
        periodEl.textContent = `${model.monthLabel} · 12:00 AM America/New_York`;

        const valOf = (entry) => entry ? formatSpendValue(isUsd ? entry.usd : entry.credits, spendUnit) : null;
        document.getElementById('cost-spend-val-viper').textContent = valOf(model.latest.viper)
            ?? (model.viperAvailable ? 'No data yet' : 'Unavailable');
        document.getElementById('cost-spend-val-total').textContent = valOf(model.latest.total)
            ?? (model.historyAvailable ? 'No snapshot yet' : 'Unavailable');
        document.getElementById('cost-spend-val-others').textContent = valOf(model.latest.others)
            ?? (model.latest.total ? 'Unavailable' : 'No snapshot yet');

        const svg = document.getElementById('cost-spend-svg');
        const emptyEl = document.getElementById('cost-spend-empty');
        const readoutEl = document.getElementById('cost-spend-readout');
        const summaryEl = document.getElementById('cost-spend-table-summary');
        const tbody = document.querySelector('#cost-monthly-spend-table tbody');

        document.getElementById('cost-spend-th-viper').textContent = `Viper (${unitShort})`;
        document.getElementById('cost-spend-th-total').textContent = `Total GitHub Copilot (${unitShort})`;
        document.getElementById('cost-spend-th-others').textContent = `Everyone else (${unitShort})`;
        document.getElementById('cost-spend-table-caption').textContent =
            `Daily cumulative monthly spend for ${model.monthLabel} in ${isUsd ? 'consumption USD' : 'Copilot credits'} (12:00 AM America/New_York boundaries)`;

        if (model.state !== 'available' || !model.days.length) {
            selectedDayIdx = -1;
            svg.innerHTML = '';
            svg.setAttribute('hidden', '');
            emptyEl.hidden = false;
            emptyEl.textContent = model.state === 'unavailable'
                ? 'Monthly spend data is unavailable right now.'
                : `No monthly spend data recorded for ${model.monthLabel} yet.`;
            readoutEl.textContent = '';
            summaryEl.textContent = 'Daily cumulative breakdown table (0 days)';
            tbody.innerHTML = '';
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.className = 'cost-empty-cell';
            td.setAttribute('colspan', '4');
            td.textContent = model.state === 'unavailable' ? 'Unavailable' : 'No data';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        if (selectedDayIdx < 0 || selectedDayIdx >= model.days.length) {
            selectedDayIdx = model.days.length - 1;
        }
        svg.removeAttribute('hidden');
        emptyEl.hidden = true;
        const canvasW = document.getElementById('cost-spend-canvas')?.clientWidth || CHART_W;
        const rendered = monthlySpendSvg(model, spendUnit, selectedDayIdx, { width: canvasW });
        svg.setAttribute('viewBox', `0 0 ${rendered.width} ${rendered.height}`);
        svg.setAttribute('aria-label', rendered.ariaLabel);
        svg.innerHTML = rendered.svgHtml;

        const activeDay = model.days[selectedDayIdx];
        const vVal = isUsd ? activeDay.viperUsd : activeDay.viperCredits;
        const tVal = isUsd ? activeDay.totalUsd : activeDay.totalCredits;
        const oVal = isUsd ? activeDay.othersUsd : activeDay.othersCredits;
        readoutEl.textContent = `${activeDay.label} (${activeDay.date}) · Viper: ${formatSpendValue(vVal, spendUnit)} · Total Copilot: ${formatSpendValue(tVal, spendUnit)} · Everyone else: ${formatSpendValue(oVal, spendUnit)}`;

        summaryEl.textContent = `Daily cumulative breakdown table (${model.days.length} ${model.days.length === 1 ? 'day' : 'days'})`;
        tbody.innerHTML = '';
        for (const day of model.days) {
            const tr = document.createElement('tr');
            const th = document.createElement('th');
            th.scope = 'row';
            th.className = 'cost-cell-name';
            th.textContent = `${day.label} (${day.date})`;
            const tdV = document.createElement('td');
            tdV.className = 'cost-cell-right';
            tdV.textContent = formatSpendValue(isUsd ? day.viperUsd : day.viperCredits, spendUnit);
            const tdT = document.createElement('td');
            tdT.className = 'cost-cell-right';
            tdT.textContent = formatSpendValue(isUsd ? day.totalUsd : day.totalCredits, spendUnit);
            const tdO = document.createElement('td');
            tdO.className = 'cost-cell-right';
            tdO.textContent = formatSpendValue(isUsd ? day.othersUsd : day.othersCredits, spendUnit);
            tr.append(th, tdV, tdT, tdO);
            tbody.appendChild(tr);
        }
    }

    function selectDay(index) {
        if (!currentSpendModel?.days?.length) return;
        const next = Math.max(0, Math.min(currentSpendModel.days.length - 1, Number(index)));
        if (!Number.isInteger(next) || next === selectedDayIdx) return;
        const restoreFocus = spendSvgEl.contains(document.activeElement);
        selectedDayIdx = next;
        renderMonthlySpend();
        if (restoreFocus) {
            spendSvgEl.querySelector(`[data-day-index="${next}"]`)?.focus({ preventScroll: true });
        }
    }

    const spendSvgEl = document.getElementById('cost-spend-svg');
    spendSvgEl.addEventListener('pointerover', (event) => {
        const idx = event.target?.dataset?.dayIndex;
        if (idx !== undefined) selectDay(idx);
    });
    spendSvgEl.addEventListener('click', (event) => {
        const idx = event.target?.dataset?.dayIndex;
        if (idx !== undefined) selectDay(idx);
    });
    spendSvgEl.addEventListener('focusin', (event) => {
        const idx = event.target?.dataset?.dayIndex;
        if (idx !== undefined) selectDay(idx);
    });

    page.querySelectorAll('[data-spend-unit]').forEach((btn) => {
        btn.addEventListener('click', () => {
            const nextUnit = btn.dataset.spendUnit === 'usd' ? 'usd' : 'credits';
            if (nextUnit === spendUnit) return;
            spendUnit = nextUnit;
            page.querySelectorAll('[data-spend-unit]').forEach((other) => {
                const active = other.dataset.spendUnit === spendUnit;
                other.classList.toggle('is-active', active);
                other.setAttribute('aria-pressed', active ? 'true' : 'false');
            });
            renderMonthlySpend();
        });
    });

    bindCopilotCosts(page, {
        state,
        onSnapshot(snapshot) {
            latestLiveCopilot = snapshot;
            if (latestDailySpend !== null || latestCopilotHistory !== null || snapshot?.status === 'fresh') {
                renderMonthlySpend();
            }
        },
    });

    const spendCanvasEl = document.getElementById('cost-spend-canvas');
    let spendResizeObserver = null;
    let lastCanvasWidth = 0;
    if (typeof ResizeObserver === 'function' && spendCanvasEl) {
        spendResizeObserver = new ResizeObserver(() => {
            const nextWidth = Math.round(spendCanvasEl.clientWidth || 0);
            if (nextWidth > 0 && nextWidth !== lastCanvasWidth && currentSpendModel?.state === 'available') {
                lastCanvasWidth = nextWidth;
                renderMonthlySpend();
            }
        });
        spendResizeObserver.observe(spendCanvasEl);
        window.addEventListener('pagehide', (event) => {
            if (event?.persisted) return;
            spendResizeObserver?.disconnect();
            spendResizeObserver = null;
        }, { once: true });
    }

    function renderBreakdownTable(tableId, data, totalCost, emptyLabel = 'No data') {
        const tbody = document.querySelector('#' + tableId + ' tbody');
        tbody.innerHTML = '';
        const cell = (className, text, attrs = {}) => {
            const td = document.createElement('td');
            td.className = className;
            td.textContent = text;
            Object.entries(attrs).forEach(([key, value]) => td.setAttribute(key, value));
            return td;
        };
        for (const [name, info] of Object.entries(data)) {
            const pct = totalCost > 0 ? (info.cost / totalCost * 100) : 0;
            const tr = document.createElement('tr');
            const bar = document.createElement('progress');
            bar.className = 'cost-bar';
            bar.max = 100;
            bar.value = Math.min(100, pct);
            const tdBar = document.createElement('td');
            tdBar.className = 'cost-bar-cell';
            tdBar.appendChild(bar);
            tr.append(
                cell('cost-cell-name', name, { title: name }),
                cell('cost-cell-right', info.calls),
                cell('cost-cell-right', costBucketPresentation(info)),
                tdBar,
            );
            tbody.appendChild(tr);
        }
        if (Object.keys(data).length === 0) {
            const tr = document.createElement('tr');
            tr.appendChild(cell('cost-empty-cell', emptyLabel, { colspan: '4' }));
            tbody.appendChild(tr);
        }
    }

    function renderExtensionActivity(presentation) {
        const tbody = document.querySelector('#cost-by-extension tbody');
        tbody.innerHTML = '';
        document.getElementById('cost-extension-note').textContent = presentation.note
            || 'Add-ons running their own callbacks, counted apart from AI model requests.';
        const cell = (className, text) => {
            const td = document.createElement('td');
            td.className = className;
            td.textContent = text;
            return td;
        };
        for (const row of presentation.rows) {
            const tr = document.createElement('tr');
            tr.append(cell('cost-cell-name', row.name), cell('cost-cell-right', row.calls),
                cell('cost-cell-name', row.surfaces));
            tbody.appendChild(tr);
        }
        if (!presentation.rows.length) {
            const tr = document.createElement('tr');
            const td = cell('cost-empty-cell', presentation.state === 'unavailable' ? 'Unavailable' : 'No extension activity recorded');
            td.setAttribute('colspan', '3');
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }

    async function loadCosts() {
        const renderLoading = () => {
            document.getElementById('cost-known-limit').textContent = 'Loading…';
            ['cost-confirmed', 'cost-estimated', 'cost-reserved', 'cost-unresolved', 'cost-unknown',
                'cost-calls', 'cost-extension-calls', 'cost-top-model'].forEach((id) => {
                document.getElementById(id).textContent = '—';
            });
            document.getElementById('cost-final').textContent = 'Loading…';
        };
        const renderUnavailable = () => {
            ['cost-known-limit', 'cost-confirmed', 'cost-estimated', 'cost-reserved', 'cost-unresolved',
                'cost-unknown', 'cost-calls', 'cost-extension-calls', 'cost-top-model'].forEach((id) => {
                document.getElementById(id).textContent = id === 'cost-known-limit' ? 'Unavailable' : '—';
            });
            document.getElementById('cost-final').textContent = 'Unavailable';
            ['cost-by-model', 'cost-by-key', 'cost-by-model-cat', 'cost-by-task-cat']
                .forEach((id) => renderBreakdownTable(id, {}, 0, 'Unavailable'));
            renderExtensionActivity({ state: 'unavailable', rows: [], note: 'Unavailable' });
            latestDailySpend = null;
            renderMonthlySpend();
        };
        renderLoading();
        try {
            const historyPromise = apiFetch(HISTORY_URL, { cache: 'no-store' })
                .then(r => (r.ok ? r.json() : null))
                .catch(() => null);
            const resp = await apiFetch('/api/cost-breakdown');
            const d = await resp.json();
            latestCopilotHistory = await historyPromise;
            const presentation = costDashboardPresentation(d);
            if (!resp.ok || presentation.state !== 'available') throw new Error('accounting unavailable');
            document.getElementById('cost-known-limit').textContent = presentation.knownLimit;
            document.getElementById('cost-confirmed').textContent = presentation.confirmed;
            document.getElementById('cost-estimated').textContent = presentation.estimated;
            document.getElementById('cost-reserved').textContent = presentation.reserved;
            document.getElementById('cost-unresolved').textContent = presentation.unresolved;
            document.getElementById('cost-unknown').textContent = presentation.unknown;
            document.getElementById('cost-final').textContent = presentation.final;
            document.getElementById('cost-calls').textContent = presentation.calls;
            document.getElementById('cost-extension-calls').textContent = presentation.extensionCalls;
            document.getElementById('cost-top-model').textContent = presentation.topModel;
            renderBreakdownTable('cost-by-model', d.by_model || {}, d.total_cost);
            renderBreakdownTable('cost-by-key', d.by_api_key || {}, d.total_cost);
            renderBreakdownTable('cost-by-model-cat', d.by_model_category || {}, d.total_cost);
            renderBreakdownTable('cost-by-task-cat', d.by_task_category || {}, d.total_cost);
            renderExtensionActivity(extensionActivityPresentation(d));
            latestDailySpend = d.daily_spend || null;
            selectedDayIdx = -1;
            renderMonthlySpend();
        } catch { renderUnavailable(); }
    }

    async function loadBudget() {
        try {
            const resp = await apiFetch('/api/settings', { cache: 'no-store' });
            const s = await resp.json().catch(() => ({}));
            const fields = s?._meta?.setup_contract?.budgetFields || [];
            fields.forEach((field) => {
                const input = document.getElementById(COST_BUDGET_INPUTS[field.settingKey]);
                if (!input) return;
                input.min = field.min || '0.01';
                input.step = field.step || 'any';
                if (field.default != null && !String(input.value || '').trim()) {
                    input.value = field.default;
                }
            });
            if (s.TOTAL_BUDGET != null) document.getElementById('s-budget').value = s.TOTAL_BUDGET;
            if (s.OUROBOROS_PER_TASK_COST_USD != null) document.getElementById('s-per-task-cost').value = s.OUROBOROS_PER_TASK_COST_USD;
        } catch {}
    }

    document.getElementById('btn-refresh-costs').addEventListener('click', loadCosts);

    // One timer owner for the save feedback: every new save, valid or not, cancels the pending clear first, so an
    // older acknowledgement can never erase what a newer save is saying.
    let ackTimer = 0;
    document.getElementById('btn-save-budget').addEventListener('click', async (event) => {
        const statusEl = document.getElementById('budget-save-status');
        clearTimeout(ackTimer);
        const budget = readPositiveBudget('s-budget');
        const perTask = readPositiveBudget('s-per-task-cost');
        if (budget === null || perTask === null) {
            statusEl.textContent = 'Budget values must be at least 0.01.';
            statusEl.dataset.tone = 'error';
            return;
        }
        const saveButton = event.currentTarget;
        saveButton.disabled = true;
        saveButton.setAttribute('aria-busy', 'true');
        statusEl.textContent = 'Saving…';
        statusEl.dataset.tone = 'muted';
        try {
            const resp = await apiFetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ TOTAL_BUDGET: budget, OUROBOROS_PER_TASK_COST_USD: perTask }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            const saved = budgetSavePresentation(data);
            statusEl.textContent = saved.text;
            statusEl.dataset.tone = saved.tone;
            if (saved.transient) {
                ackTimer = setTimeout(() => { statusEl.textContent = ''; delete statusEl.dataset.tone; }, SAVE_ACK_MS);
            }
            window.dispatchEvent(new CustomEvent('ouro:settings-updated', { detail: { reason: 'budget saved', source: 'costs' } }));
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
            statusEl.dataset.tone = 'error';
        } finally {
            saveButton.disabled = false;
            saveButton.removeAttribute('aria-busy');
        }
    });

    function refreshCostsPanel() {
        loadCosts();
        loadBudget();
    }

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab === 'costs' && state.activePage === 'dashboard') refreshCostsPanel();
    });
    window.addEventListener('ouro:settings-updated', (event) => {
        if (event.detail?.source === 'costs') return;
        refreshCostsPanel();
    });
}
