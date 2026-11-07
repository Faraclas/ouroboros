/** Retained on-demand activity. Live Works alone owns liveness and controls. */
import { getRoomActivity } from './api_client.js';
import { tr, fmt } from './i18n.js';
const list = value => Array.isArray(value) ? value : [];
export const activityTime = value => typeof value === 'number' ? value * 1000 : Date.parse(String(value || '')) || 0;
const when = value => activityTime(value) ? new Date(activityTime(value)).toLocaleString() : tr('activity.time_unknown', 'Time not recorded');
const outcomeWord = entry => ({ done: 'Done', warn: 'Done with warnings', error: 'Failed', cancelled: 'Stopped',
    timeout: 'Timed out', lifecycle_error: 'Failed' }[entry?.phase] || 'Current state not confirmed here');

export function historyWork(task) {
    const entry = list(task.entries).find(item => item.kind === 'task');
    if (!entry) return null;
    const finished = ['done', 'warn', 'error', 'cancelled', 'timeout', 'lifecycle_error'].includes(entry.phase);
    return { id: task.task_id, episode: task.task_id, kind: 'task', history: task,
        startedAt: activityTime(task.at) / 1000, work: null, confirmed: false, online: false, cmid: '',
        activity: '', attempt: null, latest: false, finished,
        view: { word: tr(`activity.outcome.${entry.phase}`, outcomeWord(entry)), secondary: '', focus: '',
            tone: 'ended', mark: finished ? 'none' : 'ring', owner: false, outcome: entry.phase || '' } };
}

/** No timer or hidden reader. One in-flight page, explicit older/retry, existing socket facts invalidate the head. */
export function createActivityHistory({ chatId, changed, fetchPage = getRoomActivity }) {
    const tasks = new Map();
    const gaps = new Set();
    let next = null, view = '', message = '', busy = false, active = false, disposed = false, paged = false;
    let controller = null, generation = 0, signature = '', refreshAgain = false;
    async function read(older = false) {
        if (disposed || !active || (older && !next)) return;
        if (busy) { if (!older) refreshAgain = true; return; }
        busy = true;
        const mine = ++generation;
        controller = new AbortController();
        changed();
        try {
            const page = await fetchPage(chatId, { cursor: older ? next : '', signal: controller.signal });
            if (mine !== generation || !active || disposed) return;
            if (older && view && page.view !== view) {
                // A cursor belongs to one membership view. Do not substitute an old tail for its head.
                refreshAgain = true;
                return;
            }
            if (view && page.view !== view) { tasks.clear(); gaps.clear(); paged = false; }
            view = page.view || '';
            for (const task of list(page.tasks)) tasks.set(task.task_id, task);
            // Same-view head updates enrich the retained list, not rewind its deeper tail.
            if (older || !paged) next = page.next_cursor || null;
            if (older) paged = true;
            for (const gap of list(page.gaps)) gaps.add(String(gap));
            message = '';
            if (!message && !page.coverage?.reached_start && !list(page.tasks).length)
                message = tr('activity.sparse', 'No matching activity in this window; older records may still exist.');
        } catch (error) {
            if (mine !== generation || error?.name === 'AbortError' || disposed) return;
            if (error?.status === 409 && older) refreshAgain = true;
            message = error?.status === 404 ? tr('activity.unavailable', 'Retained activity is unavailable on this core.')
                : String(error?.message || tr('activity.failed', 'Activity could not be read. Retry this window.'));
        } finally {
            if (mine === generation && !disposed) {
                busy = false; controller = null; changed();
                if (refreshAgain && active) { refreshAgain = false; void read(); }
            }
        }
    }
    return {
        rows: () => [...tasks.values()],
        task: id => tasks.get(id),
        state: () => ({ busy, next, message: [...gaps, message].filter(Boolean).join(' · '), view }),
        open() { active = true; void read(); },
        close() { active = false; generation += 1; controller?.abort(); controller = null; busy = false; refreshAgain = false; },
        refresh: () => read(false), older: () => read(true),
        sync(works, connected = true) {
            const current = JSON.stringify(list(works).map(work => [work.work_id, work.episode?.task_id,
                work.activity, work.transition, work.outcome, list(work.tails).map(t => [t.kind, t.state, t.service_id, t.started_at, t.returncode]) ]));
            const state = `${connected}|${current}`;
            const moved = signature && signature !== state;
            signature = state;
            if (active && connected && moved) void read();
        },
        destroy() { disposed = true; generation += 1; controller?.abort(); tasks.clear(); },
    };
}

/** Stable keyed child details: service starts never collapse by display name. */
export function paintActivityEntries(host, tasks, work, doc) {
    const old = host._activityViews || (host._activityViews = new Map());
    const byKey = new Map();
    for (const task of tasks) for (const entry of list(task?.entries)) if (entry.kind !== 'task') byKey.set(entry.key, entry);
    for (const tail of list(work?.tails).filter(t => t.kind === 'service')) {
        const key = `service:${tail.task_id}:${tail.service_id}:${tail.started_at}`;
        const kept = byKey.get(key);
        const ended = state => ['ended', 'exited'].includes(state) ? 'ended'
            : ['live', 'running'].includes(state) ? 'live' : state;
        const same = kept && ended(kept.service?.state) === ended(tail.state) && kept.service?.returncode === tail.returncode;
        byKey.set(key, { ...kept, key, kind: 'service', name: tail.name, at: tail.started_at,
            assessment: same ? kept.assessment : undefined, error: same && kept.error,
            service: tail, current: work.confirmed !== false });
    }
    const entries = [...byKey.values()].sort((a, b) => activityTime(b.at) - activityTime(a.at) || a.key.localeCompare(b.key));
    const keys = new Set(entries.map(entry => entry.key));
    for (const [key, node] of old) if (!keys.has(key)) { node.remove(); old.delete(key); }
    entries.forEach((entry, index) => {
        let node = old.get(entry.key);
        if (!node) {
            node = doc.createElement('details'); node.className = 'rac-entry'; node.dataset.key = entry.key;
            const summary = doc.createElement('summary');
            const name = doc.createElement('span'); name.className = 'rac-name';
            const state = doc.createElement('span'); state.className = 'rac-state';
            const label = doc.createElement('span'); label.className = 'rac-error-label';
            const body = doc.createElement('div'); body.className = 'rac-detail';
            summary.append(name, state, label); node.append(summary, body);
            node._parts = { name, state, label, body }; old.set(entry.key, node);
        }
        const service = entry.service;
        const current = Boolean(entry.current);
        const running = current && service?.state === 'live';
        node.dataset.activity = running ? 'working' : 'ended';
        node.dataset.error = entry.error ? entry.assessment ? 'assessed' : 'unassessed' : '';
        const word = entry.kind === 'check' ? tr('activity.check_failed', 'Check failed') : running
            ? tr('activity.service_running', 'Running') : service?.state === 'ended' || service?.state === 'exited'
                ? tr('activity.service_exited', 'Exited') : tr('activity.last_recorded', 'Last recorded; current state unconfirmed');
        const label = entry.error && entry.assessment ? tr('activity.assessed_error', 'Error — assessed') : '';
        const detail = [fmt('Started: {at}', { at: when(entry.at) })];
        if (service?.observed_at) detail.push(fmt('Observed: {at}', { at: when(service.observed_at) }));
        if (service?.returncode !== undefined && service.returncode !== null) detail.push(fmt('Process exit: {code}', { code: service.returncode }));
        if (service?.service_id) detail.push(fmt('Service: {id}', { id: service.service_id }));
        if (service?.task_id) detail.push(fmt('Starter: {id}', { id: service.task_id }));
        if (typeof service?.output_bytes === 'number') detail.push(fmt('Output: {bytes} bytes — a file observation, not progress', { bytes: service.output_bytes }));
        if (service?.output_updated_at) detail.push(fmt('Output modified: {at}', { at: when(service.output_updated_at) }));
        if (entry.assessment) detail.push(fmt('Evaluated by {who} at {at}: {reason}', { who: entry.assessment.by?.task_id || entry.assessment.by?.kind,
            at: when(entry.assessment.at), reason: entry.assessment.reason }));
        const parts = node._parts;
        if (parts.name.textContent !== entry.name) parts.name.textContent = entry.name;
        if (parts.state.textContent !== word) parts.state.textContent = word;
        if (parts.label.textContent !== label) parts.label.textContent = label;
        parts.label.hidden = !label;
        const text = detail.join('\n');
        if (parts.body.textContent !== text) parts.body.textContent = text;
    });
    reconcileActivityOrder(host, entries.map(entry => old.get(entry.key)), doc);
    host.hidden = !entries.length;
}

/** Preserve reader focus and exact range endpoints on engines without state-preserving DOM moves. */
export function reconcileActivityOrder(host, nodes, doc) {
    const active = host.contains(doc.activeElement) ? doc.activeElement : null;
    const selection = doc.getSelection?.();
    const range = selection?.anchorNode && host.contains(selection.anchorNode)
        ? [selection.anchorNode, selection.anchorOffset, selection.focusNode, selection.focusOffset] : null;
    let moved = false;
    nodes.forEach((node, index) => {
        if (host.children[index] === node) return;
        moved = true;
        if (typeof host.moveBefore === 'function' && node.parentNode === host) host.moveBefore(node, host.children[index] || null);
        else host.insertBefore(node, host.children[index] || null);
    });
    if (!moved) return;
    if (active?.isConnected && doc.activeElement !== active) active.focus({ preventScroll: true });
    if (range && range[0].isConnected && range[2]?.isConnected) selection.setBaseAndExtent(...range);
}

export function activityError(task) {
    const root = list(task?.entries).find(entry => entry.kind === 'task');
    return root?.error ? root.assessment ? 'assessed' : 'unassessed' : '';
}
export function roomActivityComposition(state) {
    const counts = state?.room?.counts;
    const parts = [];
    if (counts) for (const [key, label] of [['working', 'working'], ['waiting', 'waiting'], ['queued', 'queued'], ['paused', 'paused'], ['unknown', 'unconfirmed']])
        if (counts[key]) parts.push(fmt(`{count} ${label}`, { count: counts[key] }));
    const services = state?.room?.running_services || 0;
    if (services) parts.push(fmt('{count} running services', { count: services }));
    return parts.join(' · ');
}
