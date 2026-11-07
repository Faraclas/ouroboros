/** A paginated Parent editor, separate from Description/Notes and their exact-content CAS. */
import { fetchJson } from './api_client.js';
import { parentId } from './project_hierarchy.js';
const el = (tag, cls, text = '') => { const node = document.createElement(tag); node.className = cls; node.textContent = text; return node; };
const button = (text, run) => { const node = el('button', 'btn btn-default', text); node.type = 'button'; node.addEventListener('click', run); return node; };
const projectPrefix = id => `/api/projects/${encodeURIComponent(id)}/`;

export function projectPath(project) {
    if (!parentId(project)) return 'Top level';
    const path = Array.isArray(project.parent_path) ? project.parent_path.map(p => p.name || p.id).join(' / ') : '';
    return path && project.parent_path_complete === true ? path : `${path ? path + ' / ' : ''}Parent: ${parentId(project)} (path unavailable)`;
}

export function createParentSection({ project, signal, isCurrent, onSelect, onBrowse, onApplied }) {
    const root = el('section', 'pm-section pm-parent'); root.dataset.pmParent = '';
    const heading = el('div', 'pm-content-heading'); heading.append(el('h3', '', 'Organization'));
    const status = el('p', 'pm-note'); status.dataset.pmParentStatus = ''; status.setAttribute('role', 'status');
    const path = el('nav', 'pm-parent-path'); path.setAttribute('aria-label', 'Project path');
    const current = el('p', 'pm-parent-current', `Parent · ${projectPath(project)}`);
    const children = button('Browse children', () => onBrowse(project.id, project.name || project.id)); children.dataset.pmChildren = '';
    const chooser = el('div', 'pm-parent-chooser'); chooser.hidden = true;
    let desired = parentId(project), offset = 0, pageSize = 20, query = '', sequence = 0, busy = false, unknown = false;
    let searchController = null, writePending = false;
    const request = extra => ({ cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]), ...extra });
    const change = button('Change parent', () => { chooser.hidden = !chooser.hidden; change.setAttribute('aria-expanded', String(!chooser.hidden)); if (!chooser.hidden) { input.focus(); void load(); } });
    change.dataset.pmParentEdit = ''; change.setAttribute('aria-expanded', 'false');
    if (!['active', 'tombstoned'].includes(project.lifecycle)) change.disabled = true;
    heading.append(change);
    const form = el('form', 'pm-parent-search');
    const label = el('label', '', 'Find an active parent'); const input = el('input', 'ui-control'); input.type = 'search'; input.dataset.pmParentSearch = ''; label.append(input);
    const find = button('Search', () => {}); find.type = 'submit'; form.append(label, find);
    const candidates = el('div', 'pm-parent-candidates'); candidates.setAttribute('aria-label', 'Parent choices');
    const chosen = el('p', 'pm-note', `Selected parent: ${desired || 'Top level'}`);
    const choose = (id, name) => { if (busy || unknown) return; desired = id; chosen.textContent = `Selected parent: ${name || 'Top level'}`; update(); for (const row of candidates.querySelectorAll('[data-parent-choice]')) row.setAttribute('aria-pressed', String(row.dataset.parentChoice === id)); };
    const rootChoice = button('No parent · top level', () => choose('', 'Top level')); rootChoice.dataset.parentChoice = '';
    const page = el('span', 'pm-note');
    const previous = button('Previous', () => { offset = Math.max(0, offset - pageSize); void load(); });
    const next = button('Next', () => { offset += pageSize; void load(); });
    const pagination = el('nav', 'pm-parent-pagination'); pagination.setAttribute('aria-label', 'Parent choice pages'); pagination.append(previous, page, next);
    const save = button('Save parent', async () => {
        if (busy || unknown || desired === parentId(project)) return;
        busy = true; writePending = true; status.textContent = 'Saving parent…'; update();
        try {
            const data = await fetchJson(projectPrefix(project.id) + 'update', request({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parent_id: desired }) }), { rejectOkFalse: true });
            if (signal.aborted) return;
            if (data?.project?.id !== project.id || typeof data.project.parent_id !== 'string' || data.project.parent_id !== desired) throw Error('Saved parent was not confirmed.');
            writePending = false; status.textContent = 'Parent saved.'; project = data.project; chooser.hidden = true; change.setAttribute('aria-expanded', 'false'); paintSaved();
            try { await onApplied(); } catch (error) { if (!signal.aborted) status.textContent = `Parent saved. Refresh unavailable: ${error.message}`; }
            if (!signal.aborted && isCurrent()) change.focus({ preventScroll: true });
        } catch (error) {
            writePending = false;
            if (signal.aborted) return;
            unknown = !(error.status >= 400 && error.status < 500);
            status.textContent = unknown ? `Parent change not confirmed: ${error.message} Check the current parent before trying again.` : `Parent not changed: ${error.message} Choose an active parent or refresh the choices.`;
        } finally { busy = false; writePending = false; update(); }
    }); save.dataset.pmParentSave = '';
    const check = button('Check current parent', async () => {
        if (busy) return; busy = true; update(); status.textContent = 'Checking current parent…';
        try {
            const data = await fetchJson(projectPrefix(project.id) + 'detail', request());
            if (signal.aborted) return;
            if (data?.project?.id !== project.id || typeof data.project.parent_id !== 'string') throw Error('Current parent unavailable.');
            const currentId = data.project.parent_id; unknown = false; project = data.project;
            status.textContent = currentId === desired ? 'Current parent matches your choice.' : `Current parent: ${currentId || 'Top level'}. Your choice is kept; Save parent is a deliberate new change.`;
            paintSaved();
            try { await onApplied({ checked: true }); } catch (error) { if (!signal.aborted) status.textContent += ` Refresh unavailable: ${error.message}`; }
        } catch (error) { if (!signal.aborted) status.textContent = `Current parent unavailable: ${error.message}`; }
        finally { busy = false; update(); }
    }); check.dataset.pmParentCheck = '';
    const cancel = button('Cancel', () => { chooser.hidden = true; change.setAttribute('aria-expanded', 'false'); change.focus(); });
    function update() { save.disabled = busy || unknown || change.disabled || desired === parentId(project); check.hidden = !unknown; check.disabled = busy; cancel.disabled = busy; rootChoice.disabled = busy || unknown; rootChoice.setAttribute('aria-pressed', String(desired === '')); for (const node of candidates.querySelectorAll('button')) node.disabled = busy || unknown; }
    function paintSaved() {
        current.textContent = `Parent · ${projectPath(project)}`;
        path.replaceChildren(button('Top level', () => onBrowse('', 'Top level')));
        for (const ancestor of Array.isArray(project.parent_path) ? project.parent_path : []) path.append(el('span', '', '/'), button(ancestor.name || ancestor.id, () => onSelect(ancestor.id)));
        if (parentId(project) && project.parent_path_complete !== true) path.append(button(`Parent: ${parentId(project)} · path unavailable`, () => onSelect(parentId(project))));
    }
    function updateProject(next) {
        // A detail read begun during a write may precede its commit. Neither that
        // snapshot nor ordinary navigation can settle an outstanding write.
        if (!writePending && !unknown) {
            project = next;
            if (chooser.hidden) {
                desired = parentId(project);
                chosen.textContent = `Selected parent: ${(Array.isArray(project.parent_path) ? project.parent_path.at(-1)?.name : '') || desired || 'Top level'}`;
            }
            paintSaved();
        }
        change.disabled = !['active', 'tombstoned'].includes(next.lifecycle);
        update();
    }
    async function load() {
        searchController?.abort(); searchController = new AbortController(); const currentSequence = ++sequence;
        candidates.textContent = 'Loading active projects…'; previous.disabled = next.disabled = true;
        try {
            const params = new URLSearchParams({ lifecycle: 'active', q: query, offset: String(offset), limit: String(pageSize) });
            const data = await fetchJson(`/api/projects/catalog?${params}`, { ...request(), signal: AbortSignal.any([signal, searchController.signal, AbortSignal.timeout(30000)]) });
            if (signal.aborted || currentSequence !== sequence) return;
            if (!Array.isArray(data.projects) || !Number.isInteger(data.total) || !Number.isInteger(data.offset) || typeof data.has_more !== 'boolean') throw Error('Parent choices response is incomplete.');
            offset = data.offset; if (Number.isInteger(data.limit) && data.limit > 0) pageSize = data.limit;
            candidates.replaceChildren();
            for (const candidate of data.projects) {
                if (candidate.id === project.id) continue;
                const node = button('', () => choose(candidate.id, candidate.name || candidate.id)); node.className += ' pm-parent-choice'; node.dataset.parentChoice = candidate.id;
                node.append(el('strong', '', candidate.name || candidate.id), el('span', 'pm-project-id', candidate.id), el('span', 'pm-note', projectPath(candidate)));
                node.setAttribute('aria-pressed', String(desired === candidate.id)); candidates.append(node);
            }
            if (!candidates.childElementCount) candidates.append(el('p', 'pm-empty', 'No other active projects on this page.'));
            page.textContent = data.total ? `${offset + 1}–${offset + data.projects.length} of ${data.total}` : 'No matches';
            previous.disabled = offset === 0; next.disabled = !data.has_more; update();
        } catch (error) {
            if (signal.aborted || currentSequence !== sequence || searchController.signal.aborted) return;
            candidates.replaceChildren(el('p', 'pm-error', `Parent choices unavailable: ${error.message}`), button('Retry', load)); page.textContent = 'Page unavailable';
        }
    }
    form.addEventListener('submit', event => { event.preventDefault(); query = input.value.trim(); offset = 0; void load(); });
    const actions = el('div', 'pm-content-actions'); actions.append(save, check, cancel);
    chooser.append(form, rootChoice, candidates, pagination, chosen, actions);
    root.append(heading, path, current, children, chooser, status); paintSaved(); update(); return { root, updateProject };
}
