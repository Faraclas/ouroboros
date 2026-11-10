/** Optional starting-folder configuration. Per-room draft/request lifetime belongs to the manager. */
import { apiClient, fetchJson } from './api_client.js';
const el = (tag, cls = '', text = '') => { const n = document.createElement(tag); n.className = cls; n.textContent = text; return n; };
const button = (text, run) => { const n = el('button', 'btn btn-default', text); n.type = 'button'; n.addEventListener('click', run); return n; };
const same = (a, b) => !!a && !!b && ['working_dir', 'routing_generation', 'routing_incarnation', 'created_at'].every(key => a[key] === b[key]);
export function createLocationSection({ project, signal, isCurrent, onApplied }) {
    const root = el('section', 'pm-section pm-location'); root.dataset.pmLocation = '';
    const heading = el('div', 'pm-content-heading'), saved = el('p', 'pm-path'); saved.dataset.i18nAuthored = '';
    const observation = el('p', 'pm-note'), feedback = el('p', 'pm-note'); feedback.setAttribute('role', 'status'); feedback.dataset.pmLocationStatus = '';
    const editor = el('div', 'pm-location-editor pm-content-form'); editor.hidden = true;
    let base = null, busy = false, unknown = false, conflict = false, latestKnown = true, browserSequence = 0;
    const request = () => ({ cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]) });
    const desired = () => mode.value === 'none' ? '' : input.value.trim();
    const edit = button('Set up working location…', () => {
        base = { ...project.location }; input.value = project.working_dir || ''; mode.value = input.value ? 'folder' : 'none';
        editor.hidden = false; feedback.textContent = ''; conflict = unknown = false; update(); mode.focus();
    }); edit.dataset.pmLocationEdit = ''; edit.setAttribute('aria-expanded', 'false');
    heading.append(el('h3', '', 'Starting folder'), edit);
    const modeLabel = el('label', 'ui-field', 'Future room-default work');
    const mode = el('select', 'ui-control'); mode.dataset.pmLocationMode = '';
    for (const [value, text] of [['folder', 'Use an existing folder or checkout'], ['none', 'No starting folder']]) { const option = el('option', '', text); option.value = value; mode.append(option); }
    modeLabel.append(mode);
    const pathLabel = el('label', 'ui-field', 'Folder visible to this server');
    const input = el('input', 'ui-control'); input.type = 'text'; input.placeholder = '/path/to/existing/folder'; input.dataset.pmLocationPath = ''; input.dataset.i18nAuthored = ''; pathLabel.append(input);
    const summary = el('p', 'pm-note'); summary.dataset.pmLocationSummary = '';
    const browser = el('div', 'new-project-browser'); browser.hidden = true;
    const browsePath = el('p', 'pm-path'), dirs = el('div', 'new-project-browser-list'); browser.append(browsePath, dirs);
    const browse = button('Browse home folders', () => { browser.hidden = !browser.hidden; if (!browser.hidden) void loadDirs(''); }); browse.dataset.pmLocationBrowse = '';
    async function loadDirs(path) {
        const sequence = ++browserSequence; dirs.textContent = 'Loading folders…';
        try {
            const data = await fetchJson(`/api/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ''}`, request());
            if (signal.aborted || sequence !== browserSequence) return;
            browsePath.textContent = data.path; dirs.replaceChildren();
            const choose = button('Select this folder', () => { input.value = data.path; mode.value = 'folder'; browser.hidden = true; update(); input.focus(); });
            choose.disabled = busy || unknown; dirs.append(choose);
            if (data.parent) dirs.append(button('.. (up)', () => void loadDirs(data.parent)));
            for (const dir of data.dirs || []) { const n = button(dir.name, () => void loadDirs(dir.path)); n.classList.add('new-project-dir-row'); dirs.append(n); }
            if (data.truncated) dirs.append(el('p', 'pm-note', 'More folders exist; showing the first 500. You can enter a path directly.'));
        } catch (error) { if (!signal.aborted && sequence === browserSequence) dirs.textContent = `Cannot browse: ${error.message}. Enter a server-visible path directly.`; }
    }
    const apply = button('Apply', async () => {
        if (apply.disabled) return;
        const target = desired(); busy = true; feedback.textContent = 'Applying starting folder…'; update();
        try {
            const data = await apiClient.projectLocationUpdate(project.id, target, base, request().signal);
            if (signal.aborted) return;
            if (data?.project?.id !== project.id || typeof data.project.location?.working_dir !== 'string') throw Error('Saved location response is incomplete.');
            project = data.project; base = { ...project.location }; unknown = conflict = false; editor.hidden = true;
            feedback.textContent = 'Starting folder saved. Existing work and resource associations are unchanged.'; paintSaved();
            busy = false; update(); await changed(); if (isCurrent()) edit.focus({ preventScroll: true });
        } catch (error) {
            if (signal.aborted) return;
            if (error.status === 409 && error.body?.reason_code === 'project_location_conflict') {
                conflict = true; latestKnown = false; feedback.textContent = 'The saved location or Project identity changed. Your choice is kept. Check the current location before continuing.';
            } else {
                unknown = !error.status || error.status >= 500;
                feedback.textContent = unknown ? `Apply was not confirmed: ${error.message}. Check current location before any retry.` : `Not applied: ${error.message}. Your choice is kept.`;
            }
        } finally { busy = false; if (!signal.aborted) update(); }
    }); apply.classList.replace('btn-default', 'btn-primary'); apply.dataset.pmLocationApply = '';
    async function changed() { try { await onApplied(); } catch (error) { if (!signal.aborted) feedback.textContent += ` Refresh unavailable: ${error.message}`; } }
    const check = button('Check current location', async () => {
        if (busy) return; busy = true; update();
        try {
            const data = await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/detail`, request());
            if (signal.aborted) return;
            if (data?.project?.id !== project.id || !data.project.location) throw Error('Current location response is incomplete.');
            project = data.project; latestKnown = true; unknown = false; conflict = !same(base, project.location); paintSaved();
            feedback.textContent = project.working_dir === desired() ? 'Current saved location matches your choice. No retry is needed.' : 'Current location checked. Your choice is kept; review it before a new Apply.';
            if (project.working_dir === desired()) { base = { ...project.location }; conflict = false; }
        } catch (error) { latestKnown = false; if (!signal.aborted) feedback.textContent = `Current location unavailable: ${error.message}. Your choice is kept.`; }
        finally { busy = false; if (!signal.aborted) update(); }
    }); check.dataset.pmLocationCheck = '';
    const continueEdit = button('Use current version as baseline', () => {
        base = { ...project.location }; conflict = false; feedback.textContent = 'Your choice is kept. Apply will compare this current version again.'; update(); input.focus();
    }); continueEdit.dataset.pmLocationContinue = '';
    const cancel = button('Cancel', () => {
        editor.hidden = true; ++browserSequence; browser.hidden = true; feedback.textContent = unknown ? 'Apply outcome remains unconfirmed. Check current location before editing again.' : ''; update(); edit.focus();
    }); cancel.dataset.pmLocationCancel = '';
    const actions = el('div', 'pm-content-actions'); actions.append(apply, cancel, continueEdit);
    editor.append(el('p', 'pm-note', 'New room-default work can read, write and run commands in the selected folder. Existing live or saved work stays at its bound target. Resources and files are unchanged; nothing resumes.'), modeLabel, pathLabel, browse, browser, summary, actions);
    root.append(heading, saved, observation, editor, feedback, check);
    function paintSaved() {
        saved.textContent = project.working_dir || 'No starting folder configured';
        const fact = project.working_dir_observation;
        observation.textContent = !project.working_dir ? 'A project can be conversation only.' : fact?.status === 'available' ? (fact.kind === 'directory' ? 'Folder available' : 'Not a folder') : fact?.status === 'missing' ? 'Missing from this environment' : 'Inspection unavailable';
    }
    function update() {
        const editable = ['active', 'tombstoned'].includes(project.lifecycle) && !!project.location;
        edit.disabled = busy || unknown || !editable; edit.setAttribute('aria-expanded', String(!editor.hidden));
        mode.disabled = input.disabled = busy || unknown; pathLabel.hidden = browse.hidden = mode.value === 'none';
        if (mode.value === 'none') browser.hidden = true;
        apply.disabled = busy || unknown || conflict || !editable || (mode.value === 'folder' && !desired()) || (desired() === base?.working_dir && project.working_dir_configured === true);
        apply.textContent = busy ? 'Applying…' : 'Apply'; cancel.disabled = busy;
        check.hidden = !unknown && !conflict; check.disabled = busy;
        continueEdit.hidden = !conflict || unknown; continueEdit.disabled = busy || !latestKnown;
        summary.textContent = `Current: ${project.working_dir || 'No starting folder'} → New default: ${desired() || 'No starting folder'}`;
        for (const node of browser.querySelectorAll('button')) node.disabled = busy || unknown;
    }
    mode.addEventListener('change', update); input.addEventListener('input', update);
    function updateProject(next) {
        if (!busy && !unknown) { project = next; latestKnown = true; if (!editor.hidden && !same(base, next.location)) conflict = true; paintSaved(); }
        update();
    }
    paintSaved(); update(); return { root, updateProject };
}
