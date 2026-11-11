/** Sleep-owned explanations and navigation only. No prerequisite evaluation or inferred edges. */
import { projectReference } from './project_reference.js';
import { tr, fmt } from './i18n.js';
import { activityWord } from './work_status.js';

export function waitTargetButton(target, trail = [], doc = document) {
    if (!target.work_id || target.state !== 'live') return null;
    if (target.project_id) return projectReference({ id: target.project_id, name: target.project_id, chat_id: target.room },
        { workId: target.task_id || target.work_id, waitTrail: trail });
    if (Number(target.room) !== 1) return null;
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm';
    button.textContent = tr('wait.open_main', 'Open in Main');
    button.addEventListener('click', () => window.dispatchEvent(new CustomEvent('ouro:open-task', {
        detail: { task_id: target.task_id || target.work_id, chat_id: 1,
            work_id: target.task_id || target.work_id, wait_trail: trail },
    })));
    return button;
}

const views = new WeakMap();
export function paintWaitDependencies(node, work, trail = []) {
    // Keyed rows keep focused controls through ordinary census/status changes.
    let state = views.get(node);
    if (!state) { state = new Map(); views.set(node, state); }
    const keep = new Set(), doc = node.ownerDocument;
    const rowFor = (key, tag = 'p') => {
        keep.add(key);
        if (!state.has(key)) {
            const element = doc.createElement(tag);
            state.set(key, element); node.append(element);
        }
        return state.get(key);
    };
    const line = (key, label, text) => {
        const row = rowFor(key);
        const value = `${label}: ${text}`;
        if (row.textContent !== value) row.textContent = value;
    };
    const reasons = work?.confirmed !== false ? work?.because || [] : [];
    reasons.forEach((reason, index) => {
        const dependency = reason.dependency, prefix = `${index}:`;
        if (dependency) {
            line(prefix + 'operation', tr('wait.operation', 'Next operation waiting for'), dependency.waiting_for);
            line(prefix + 'why', tr('wait.why', 'Why'), dependency.why);
            line(prefix + 'release', tr('wait.release', 'Can continue when'), dependency.release);
            line(prefix + 'source', tr('wait.source', 'Source'), tr('wait.authored', 'Recorded explanation for this sleep, not an automatic release check.'));
        } else if (reason.code === 'sleeping') {
            line(prefix + 'why', tr('wait.why', 'Why'), tr('wait.unknown_reason', 'No operation explanation was recorded for this sleep. Wake triggers below are not prerequisites.'));
        }
        for (const target of reason.targets || []) {
            const row = rowFor(prefix + target.task_id, 'div');
            row.className = 'rw-wait-target';
            if (!row.waitText) {
                row.waitText = doc.createElement('span'); row.append(row.waitText);
                row.waitNote = doc.createElement('span'); row.append(row.waitNote);
            }
            const word = target.state === 'live' ? activityWord({ activity: target.activity })
                : target.state === 'ended' ? tr('wait.target_ended', 'Task ended; release condition not evaluated')
                    : tr('wait.target_unknown', 'Current target state unavailable');
            row.waitText.textContent = `${target.name} — ${word}`;
            const current = { task_id: work.episode?.task_id || work.work_id, work_id: work.work_id,
                project_id: work.project_id, room: work.room, name: work.name, state: 'live' };
            const key = JSON.stringify([target.task_id, target.work_id, target.project_id, target.room,
                target.state, trail, current]);
            if (row.waitTargetKey !== key) {
                row.waitTargetKey = key;
                const focused = row.waitButton && doc.activeElement === row.waitButton;
                row.waitButton?.remove();
                row.waitButton = waitTargetButton(target, [...trail, current], doc);
                if (row.waitButton) row.append(row.waitButton);
                else if (focused) { row.tabIndex = -1; row.focus(); }
            }
            row.waitNote.textContent = dependency && !target.selected_as_wake
                ? tr('wait.not_trigger', 'Not explicitly selected as a task/mail wake trigger; other triggers may apply.') : '';
        }
    });
    for (const [key, element] of state) if (!keep.has(key)) { element.remove(); state.delete(key); }
    node.hidden = !keep.size;
}

export function paintWaitBack(node, trail = []) {
    const key = JSON.stringify(trail);
    if (node.dataset.trailKey === key) return;
    node.dataset.trailKey = key;
    node.replaceChildren();
    const target = trail.at(-1);
    if (!target) { node.hidden = true; return; }
    const label = node.ownerDocument.createElement('span');
    label.textContent = fmt('Back to waiting work: {name}', { name: target.name });
    const button = waitTargetButton(target, trail.slice(0, -1), node.ownerDocument);
    node.append(label);
    if (button) node.append(button);
    node.hidden = false;
}
