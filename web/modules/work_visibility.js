/** Personal-client presentation only. Host state is never inferred from authored prose or its age. */
import { fmt, tr } from './i18n.js';
import { reasonText, workRowView } from './work_status.js';

const list = value => Array.isArray(value) ? value : [];
const authoredClock = at => {
    if (at === null || at === undefined || at === '') return '';
    const ms = typeof at === 'number' ? at * 1000 : Date.parse(String(at));
    const date = new Date(ms);
    return Number.isFinite(date.getTime()) ? date.toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit' }) : '';
};

// New host observation times are explicit ISO instants. Only the existing reason.since accepts epoch seconds.
const instant = (at, legacy = false) => {
    const iso = typeof at === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d.*(?:Z|[+-]\d\d:\d\d)$/.test(at);
    const ms = legacy && typeof at === 'number' ? at * 1000 : iso ? Date.parse(at) : NaN;
    return Number.isFinite(ms) && Number.isFinite(new Date(ms).getTime()) ? (iso ? at : new Date(ms).toISOString())
        : tr('visibility.time_unknown', 'time unknown');
};
const literal = value => typeof value === 'string' ? value : '';
const ids = value => list(value).filter(value => typeof value === 'string' && value).join(', ');
const localInstant = (at, legacy = false) => {
    const exact = instant(at, legacy);
    return exact === tr('visibility.time_unknown', 'time unknown') ? exact : new Date(exact).toLocaleString([], {
        year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' });
};

/** One interpretation, two reading depths. Compact prose omits opaque identities, never the full disclosure. */
export function workObservations(work = {}) {
    const observations = [];
    for (const reason of list(work.because)) {
        const wake = reason?.wake;
        if (reason?.code !== 'sleeping' || !wake || !['warm', 'cold'].includes(wake.mode)) continue;
        const members = ids(reason.ids);
        const sources = [];
        const shortSources = [];
        // Put the absolute timer first in the short sentence so a long literal service name cannot bury it.
        if (wake.at !== null && wake.at !== undefined) shortSources.push(fmt('timer {at}', { at: localInstant(wake.at) }));
        if (wake.any_mail === true) {
            sources.push(tr('visibility.any_mail', 'any addressed mail'));
            shortSources.push(tr('visibility.any_mail', 'any addressed mail'));
        }
        for (const [field, template, compact] of [['senders', 'mail from {ids}', 'mail ({count} senders)'],
            ['tasks', 'task ending or attention: {ids}', 'task ending/attention ({count})'],
            ['runs', 'external run ending: {ids}', 'external run ending ({count})']]) {
            const selected = ids(wake[field]);
            if (selected) {
                sources.push(fmt(template, { ids: selected }));
                const count = list(wake[field]).filter(id => typeof id === 'string' && id).length;
                shortSources.push(fmt(compact, { count, n: count }, field === 'senders' && count === 1 ? 'mail ({count} sender)' : compact));
            }
        }
        for (const service of list(wake.services)) {
            const name = literal(service?.name) || tr('visibility.unnamed_service', 'Unnamed service');
            shortSources.push(fmt('service exit: {name}', { name }));
            sources.push(fmt('service exit: {name} [{id}], start {at}', { name,
                id: literal(service?.service_id) || tr('visibility.id_unknown', 'ID unknown'), at: instant(service?.started_at) }));
        }
        if (wake.at !== null && wake.at !== undefined) sources.push(fmt('timer at {at}', { at: instant(wake.at) }));
        const heading = at => fmt(wake.mode === 'warm' ? 'Warm sleep · since {at}' : 'Cold sleep · since {at}', { at });
        const selection = selected => selected.length ? fmt('Any one can wake it: {sources}', { sources: selected.join(` ${tr('visibility.or', 'OR')} `) })
            : tr('visibility.wake_unlisted', 'Selected wake sources not listed.');
        const fullHeading = heading(instant(reason.since, true));
        observations.push({ kind: 'sleep', summary: `${heading(localInstant(reason.since, true))}\n${selection(shortSources)}`,
            full: [members ? `${fullHeading} · ${fmt('member {ids}', { ids: members })}` : fullHeading, selection(sources),
                tr('visibility.independent_wakes', 'Owner/control mail, descendant escalations and its own external-run questions can also wake it.')].join('\n') });
    }
    for (const tail of list(work.tails)) {
        if (tail?.kind !== 'service') continue;
        const identity = literal(tail.service_id) || tr('visibility.id_unknown', 'ID unknown');
        const starter = literal(tail.task_id) || tr('visibility.id_unknown', 'ID unknown');
        const summary = reasonText(tail);
        const detail = [fmt('Service {id} · starter {task} · start {start} · observed {observed}', {
            id: identity, task: starter, start: instant(tail.started_at), observed: instant(tail.observed_at) })];
        if (tail.state === 'ended') detail.push(Number.isInteger(tail.returncode)
            ? fmt('Service exit code {code}', { code: tail.returncode }) : tr('visibility.exit_unknown', 'Service exit code unknown'));
        const bytes = typeof tail.output_bytes === 'number' && Number.isSafeInteger(tail.output_bytes) && tail.output_bytes >= 0
            ? fmt('{bytes} bytes', { bytes: tail.output_bytes }) : tr('visibility.output_unknown', 'size unknown');
        detail.push(fmt('Output file: {bytes}; modified {at} (filesystem observation, not progress)', { bytes, at: instant(tail.output_updated_at) }));
        observations.push({ kind: 'service', summary: `${summary} · ${fmt('observed {at}', { at: localInstant(tail.observed_at) })}`,
            full: `${summary}\n${detail.join(' · ')}` });
    }
    return observations;
}

/** Shared by the quiet room explanation and its existing full work disclosure. */
export function workExplanation(work = {}, { connected = true, ready = true, available = true, stopping = '' } = {}) {
    const confirmed = connected && ready && available && work.confirmed !== false;
    // Structured services are said once in observations, not duplicated in the legacy secondary sentence.
    // Global completeness describes membership only; each listed row retains its own host confirmation.
    const current = confirmed ? { ...work, tails: list(work.tails).filter(tail => tail?.kind !== 'service') }
        : { ...work, activity: 'unknown', transition: 'none', call: '',
        because: [], tails: [], turn: [], confirmed: false, mark: 'ring' };
    const row = workRowView(current, { stopping: confirmed ? stopping : '' });
    const text = typeof work.focus?.text === 'string' ? work.focus.text.trim() : '';
    const at = text ? authoredClock(work.focus?.at) : '';
    const label = text ? [confirmed ? tr('visibility.update', 'Viper update') : tr('visibility.last_update', 'Last recorded Viper update'), at || tr('visibility.time_unknown', 'time unknown')].join(' · ') : '';
    const observations = confirmed ? workObservations(work) : [];
    return { ...row, confirmed, observations, observationLine: observations.map(item => item.full).join('\n'),
        observationSummary: observations.map(item => item.summary).join('\n'), focus: text, focusLabel: label,
        focusLine: text ? `${label} — ${text}` : '',
        focusAt: text ? String(work.focus?.at ?? '') : '',
        secondary: confirmed ? row.secondary : tr('visibility.state_unknown', 'Current work state is not confirmed.') };
}

/** Counts describe the delivered membership, not another priority or a chosen "main" work. */
export function roomExplanation(state, { connected = true, ready = true, available = true } = {}) {
    const works = list(state?.works);
    const note = !connected ? tr('visibility.reconnecting', 'Reconnecting — current work state is not confirmed.')
        : !ready ? tr('visibility.starting', 'Host is starting — current work state is not confirmed.')
            : !available ? tr('visibility.unavailable', 'Work state read unavailable — showing the last returned work list.')
            : state?.complete !== true ? tr('visibility.partial', 'Work list may be incomplete.') : '';
    const words = [...new Set(works.map(work => workExplanation(work, { connected, ready, available }).word))];
    const countText = state?.complete === true && connected && ready && available ? fmt('{count} works', { count: works.length })
        : fmt('{count} known works', { count: works.length });
    const composition = works.length > 1 ? `${countText} · ${words.join(' / ')}` : '';
    return { note, composition };
}

/** The existing room disclosure owns the details; the optional under-bar view is absent.
 * Keep this factory seam for current room owners: optional render/hide/destroy and measured
 * header reserve already handle null, so no hidden DOM, observer or second lifetime is needed.
 */
export function createWorkVisibility() { return null; }
