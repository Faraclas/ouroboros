/** Dashboard's retained room catalog. Inspection never mounts a live chat or task controls. */
import { apiClient, fetchJson, jsonPost, taskArtifactDownloadUrl } from './api_client.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { mountChatMarkdown, destroyChatMarkdown } from './chat_markdown.js';
import { createDocumentReader, documentReaderKind } from './document_reader.js';
import { attachmentViews } from './chat_attachments.js';
import { createParentSection, projectPath } from './project_parent.js';

// Installation-local labels/policy, not a core lifecycle rule or a name heuristic.
export const MANAGED_PROJECT_IDS = Object.freeze(['viper-interface', 'viper-discord', 'viper-model-transport']);
export const canCloseProject = project => project?.id !== 'viper-interface';
export const projectLifecycle = project => ({ active: 'active', tombstoned: 'closed', deleting: 'closing' }[project?.lifecycle] || 'unknown');
const projectPrefix = id => `/api/projects/${encodeURIComponent(id)}/`;
const message = error => error?.message || String(error);
const title = project => project?.name || project?.id || 'Project';
const projectContent = project => ({ description: project?.description || '', notes: project?.notes || '' });
const sameContent = (a, b) => a.description === b.description && a.notes === b.notes;
// The only authored-content wire adapter; lifecycle/name/pin writes stay separate.
export function saveProjectContent(id, content, expectedContent, signal) {
    return fetchJson(projectPrefix(id) + 'update', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
        body: JSON.stringify({ description: content.description, notes: content.notes, expected_content: expectedContent }),
    }, { rejectOkFalse: true });
}
const el = (tag, className = '', text = '') => {
    const node = document.createElement(tag); node.className = className; node.textContent = text; return node;
};
function button(text, action, primary = false) {
    const node = el('button', `btn ${primary ? 'btn-primary' : 'btn-default'}`, text); node.type = 'button'; node.addEventListener('click', action); return node;
}
function rich(text) { const node = el('div', 'pm-rich'); node.dataset.i18nAuthored = ''; mountChatMarkdown(node, String(text || '')); return node; }
function cleanRich(root) { root.querySelectorAll('.pm-rich').forEach(destroyChatMarkdown); }
function deliveredFile(row) {
    const raw = String(row.download_url || '');
    let canonical = '', address = '';
    try { const u = new URL(raw, location.origin); if (u.origin === location.origin) { address = u.pathname + u.search; canonical = taskArtifactDownloadUrl(row.task_id, decodeURIComponent(u.pathname.split('/').at(-1)), u.searchParams.get('relpath') || ''); } } catch { /* No readable address on this row. */ }
    const source = raw && (address === canonical || address.startsWith('/api/files/download?')) ? address : '';
    const base64 = typeof row.file_base64 === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(row.file_base64) ? row.file_base64 : '';
    const upload = /^\/api\/files\/download\?upload=[0-9a-f]{32}_[^&#\s]+$/.test(source);
    return { source, reader: base64 ? { base64 } : source && (address === canonical || upload) ? { url: source } : null };
}

export function initProjectsManager({ mount, state, onOpen, onCreate, onChanged, onPin, onWorkItems }) {
    const root = el('section', 'projects-manager');
    root.setAttribute('aria-label', 'Projects');
    root.innerHTML = `<header class="pm-toolbar"><div class="pm-heading"><h2>Projects</h2><p>Rooms and their working context</p></div><button type="button" class="btn btn-primary" data-pm-create>New project</button></header>
        <form class="pm-filters" role="search"><label>Find a project<input class="ui-control" type="search" data-pm-search placeholder="Search name, ID or description" autocomplete="off"></label><label>Show<select class="ui-control" data-pm-lifecycle><option value="all">All projects</option><option value="active">Active</option><option value="closed">Closed</option></select></label><button class="btn btn-default" type="submit">Search</button><button class="btn btn-default" type="button" data-pm-refresh>Refresh</button></form>
        <div class="pm-catalog-status" role="status" data-pm-status></div><div class="pm-workspace"><div class="pm-catalog"><div class="pm-list" data-pm-list aria-label="Project catalog"></div><nav class="pm-pagination" aria-label="Project pages"><button type="button" class="btn btn-default" data-pm-previous>Previous</button><span data-pm-page></span><button type="button" class="btn btn-default" data-pm-next>Next</button></nav></div><section class="pm-detail" data-pm-detail aria-label="Project details"><p class="pm-empty">Select a project to inspect its history and resources.</p></section></div>`;
    mount.append(root);
    const q = selector => root.querySelector(selector), detail = q('[data-pm-detail]');
    let live = false, dead = false, rows = [], selectedId = '', selected = null, offset = 0, total = 0, more = false;
    let catalogRequest = 0, detailRequest = 0, catalogController = null, detailController = null, historyController = null;
    let search = '', lifecycle = 'all', historyCursor = null, historyBusy = false, historyInterrupted = false, mutationBusy = false;
    let pageSize = 30, parentScope = null;
    const historySeen = new Set();
    // Page memory only. Saved text lives in the registry; unsaved drafts never use browser storage.
    const contentEditors = new Map(), contentControllers = new Set();
    // Parent writes outlive detail GETs/navigation, just like per-room content drafts.
    const parentEditors = new Map(), parentController = new AbortController();
    const reader = createDocumentReader({ actions: { download(file) { const a = el('a'); a.href = file.url; a.download = file.filename; a.click(); } } });
    const isHere = () => live && !dead;
    const validSelection = id => isHere() && selectedId === id;
    const requestOptions = controller => ({ cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) });
    function errorIn(host, text, retry) {
        const box = el('div', 'pm-error'); box.setAttribute('role', 'alert'); box.append(el('p', '', text));
        if (retry) box.append(button('Retry', retry)); host.replaceChildren(box);
    }
    function selectionPaint() { root.querySelectorAll('[data-pm-row]').forEach(n => n.setAttribute('aria-pressed', String(n.dataset.pmRow === selectedId))); }
    function paintList() {
        const list = q('[data-pm-list]'); list.replaceChildren();
        for (const project of rows) {
            const row = button('', () => selectProject(project.id)); row.className = 'pm-project-row'; row.dataset.pmRow = project.id;
            const identity = el('span', 'pm-row-identity'); identity.append(el('strong', '', title(project)));
            if (project.description) { const description = el('span', 'pm-row-description', project.description); description.dataset.i18nAuthored = ''; identity.append(description); }
            identity.append(el('span', 'pm-project-id', project.id));
            identity.append(el('span', 'pm-row-parent', projectPath(project)));
            const facts = el('span', 'pm-row-facts'); facts.append(el('span', 'pm-lifecycle', projectLifecycle(project)));
            if (MANAGED_PROJECT_IDS.includes(project.id)) facts.append(el('span', 'pm-managed', 'Managed by Viper'));
            if (project.pinned) facts.append(el('span', 'pm-pin', 'Pinned'));
            row.append(identity, facts); list.append(row);
        }
        if (!rows.length) list.append(el('p', 'pm-empty', search ? 'No projects match this search.' : 'No projects in this view.'));
        q('[data-pm-page]').textContent = total ? `${offset + 1}–${offset + rows.length} of ${total}` : '0 projects';
        q('[data-pm-previous]').disabled = offset === 0;
        q('[data-pm-next]').disabled = !more;
        selectionPaint();
    }
    async function loadCatalog({ retainSelection = true } = {}) {
        if (!isHere()) return;
        catalogController?.abort(); catalogController = new AbortController(); const current = ++catalogRequest;
        q('[data-pm-status]').textContent = 'Loading projects…'; q('[data-pm-list]').setAttribute('aria-busy', 'true');
        q('[data-pm-previous]').disabled = q('[data-pm-next]').disabled = true;
        const params = new URLSearchParams({ lifecycle, q: search, offset: String(offset), limit: String(pageSize) });
        if (parentScope !== null) params.set('parent_id', parentScope.id);
        try {
            const data = await fetchJson(`/api/projects/catalog?${params}`, requestOptions(catalogController));
            if (!isHere() || current !== catalogRequest) return;
            if (!Array.isArray(data.projects) || !Number.isInteger(data.total) || !Number.isInteger(data.offset) || typeof data.has_more !== 'boolean') throw Error('Catalog response is incomplete.');
            if (Number.isInteger(data.limit) && data.limit > 0) pageSize = data.limit;
            rows = data.projects; total = data.total; offset = data.offset; more = data.has_more;
            if (!rows.length && total > 0 && offset > 0) {
                offset = Math.floor((total - 1) / pageSize) * pageSize;
                void loadCatalog(); return;
            }
            q('[data-pm-status]').textContent = ''; paintList();
            if (!retainSelection && rows.length) void selectProject(rows[0].id);
        } catch (error) {
            if (!isHere() || current !== catalogRequest || catalogController.signal.aborted) return;
            q('[data-pm-status]').textContent = 'Project catalog unavailable.';
            errorIn(q('[data-pm-list]'), message(error), () => loadCatalog());
            q('[data-pm-page]').textContent = 'Page unavailable';
        } finally { if (current === catalogRequest) q('[data-pm-list]').removeAttribute('aria-busy'); }
    }
    function stopDetailReads() {
        if (historyBusy || detail.querySelector('.pm-result-body[aria-busy="true"]')) historyInterrupted = true;
        ++detailRequest; detailController?.abort(); historyController?.abort(); reader.close({ restoreFocus: false }); historyBusy = false;
    }
    async function selectProject(id) {
        const same = selectedId === id;
        const keepDetail = same && selected;
        const draft = same ? { path: q('[data-pm-resource-path]')?.value || '', label: q('[data-pm-resource-label]')?.value || '' } : null;
        const retainedHistory = same && !historyBusy && !historyInterrupted && !detail.querySelector('.pm-result-body[aria-busy="true"]') ? detail.querySelector('.pm-history-section') : null;
        const retainedCursor = historyCursor, retainedSeen = new Set(historySeen), scroll = same ? detail.scrollTop : 0;
        stopDetailReads(); selectedId = id; if (!keepDetail) selected = null; selectionPaint();
        retainedHistory?.remove();
        if (!keepDetail) { cleanRich(detail); detail.replaceChildren(el('p', 'pm-empty', 'Loading project…')); }
        detail.setAttribute('aria-busy', 'true');
        const current = detailRequest; detailController = new AbortController();
        try {
            const data = await fetchJson(projectPrefix(id) + 'detail', requestOptions(detailController));
            if (!validSelection(id) || current !== detailRequest) return;
            if (data?.project?.id !== id || !Array.isArray(data.project.resources)) throw Error('Project detail response is incomplete.');
            selected = data.project; paintDetail();
            if (draft) { if (q('[data-pm-resource-path]')) q('[data-pm-resource-path]').value = draft.path; if (q('[data-pm-resource-label]')) q('[data-pm-resource-label]').value = draft.label; }
            if (retainedHistory) { detail.querySelector('.pm-history-section').replaceWith(retainedHistory); historyCursor = retainedCursor; for (const key of retainedSeen) historySeen.add(key); }
            detail.scrollTop = scroll;
        } catch (error) {
            if (!validSelection(id) || current !== detailRequest || detailController.signal.aborted) return;
            if (keepDetail) {
                if (retainedHistory) detail.append(retainedHistory);
                q('[data-pm-action-status]').textContent = `Project refresh unavailable: ${message(error)} Your draft is kept. Refresh to try again.`;
            } else errorIn(detail, `Project details unavailable: ${message(error)}`, () => selectProject(id));
        } finally { if (current === detailRequest) detail.removeAttribute('aria-busy'); }
    }
    async function changeProject(project, path, payload, control, label) {
        if (mutationBusy) return false;
        mutationBusy = true; control.disabled = true; const oldText = control.textContent; control.textContent = label;
        const status = q('[data-pm-action-status]'); if (status) status.textContent = '';
        let applied = false;
        try {
            await jsonPost(projectPrefix(project.id) + path.slice(1), payload);
            applied = true;
            let refreshError = '';
            try { await onChanged?.(); } catch (error) { refreshError = message(error); }
            if (validSelection(project.id)) await selectProject(project.id);
            if (isHere()) await loadCatalog();
            if (refreshError && validSelection(project.id)) q('[data-pm-action-status]').textContent = `${oldText} applied. Sidebar refresh failed: ${refreshError} Refresh to update it.`;
            return true;
        } catch (error) {
            if (validSelection(project.id)) {
                const target = q('[data-pm-action-status]'); if (target) { target.textContent = applied ? `${oldText} applied. Refresh failed: ${message(error)}` : `${oldText} was not confirmed: ${message(error)} Refresh to check the current state.`; target.setAttribute('role', 'alert'); }
            }
            return false;
        } finally { mutationBusy = false; if (control.isConnected) { control.disabled = false; control.textContent = oldText; } }
    }
    function paintDetail() {
        const project = selected, life = projectLifecycle(project); historyCursor = null; historyBusy = false; historyInterrupted = false;
        historySeen.clear();
        const previousContent = contentEditors.get(project.id)?.node;
        const focused = previousContent?.contains(document.activeElement) ? document.activeElement : null;
        const focusEdit = focused?.hasAttribute('data-pm-content-edit');
        const content = contentSection(project);
        content.remove(); // Keep the actual inputs, selection and undo state through ordinary refresh.
        cleanRich(detail); detail.replaceChildren();
        const heading = el('header', 'pm-detail-heading'), words = el('div');
        words.append(el('h2', '', title(project)), el('p', 'pm-project-id', project.id)); heading.append(words, el('span', 'pm-lifecycle', life)); detail.append(heading);
        if (MANAGED_PROJECT_IDS.includes(project.id)) detail.append(el('p', 'pm-managed-note', 'Managed by Viper · habitat resources are retained.'));
        const actions = el('div', 'pm-actions');
        if (onWorkItems) actions.append(button('Work Items', () => onWorkItems(project)));
        if (life === 'active') {
            actions.append(button('Open', () => onOpen?.(project, false), true), button('Open beside', () => onOpen?.(project, true)));
            const pin = button(project.pinned ? 'Unpin' : 'Pin', async () => { pin.disabled = true; try { await onPin?.(project, !project.pinned); if (validSelection(project.id)) await selectProject(project.id); if (isHere()) await loadCatalog(); } finally { if (pin.isConnected) pin.disabled = false; } }); actions.append(pin);
            if (canCloseProject(project)) {
                const close = button('Close project…', async () => {
                    const ok = await openConfirmDialog({ title: 'Close project', body: `Close “${title(project)}”? Unfinished work in this room will be stopped. Its immediate children move to this room’s parent, or to top level; those child rooms and their work otherwise stay unchanged. History, notes and files are kept. Recurring schedules in this room will be paused until you re-enable them in Dashboard Activity. You can reopen this room from Dashboard Projects.`, confirmLabel: 'Close project', danger: true });
                    if (ok === true) await changeProject(project, '/delete', {}, close, 'Closing…');
                }); actions.append(close);
            }
        } else if (life === 'closed') {
            const reopen = button('Reopen project', async () => {
                const ok = await openConfirmDialog({ title: 'Reopen project', body: `Reopen “${title(project)}” as the same room? Previous work will not resume. Children previously promoted will not move back. Recurring schedules stay paused until you re-enable them in Dashboard Activity. No files will be created, cloned or restored.`, confirmLabel: 'Reopen project' });
                if (ok === true) await changeProject(project, '/reopen', {}, reopen, 'Reopening…');
            }, true); actions.append(reopen);
            detail.append(el('p', 'pm-note', 'Closed room · inspect below, or reopen to continue the conversation.'));
        } else detail.append(el('p', 'pm-note', life === 'closing' ? 'Closure is in progress. Reopen becomes available after work has stopped.' : 'Project state unavailable. Refresh to check it.'));
        detail.append(actions, el('p', 'pm-action-status'));
        detail.lastElementChild.dataset.pmActionStatus = ''; detail.lastElementChild.setAttribute('role', 'status');
        detail.append(content);
        (focusEdit ? content.querySelector('[data-pm-content-edit]') : focused)?.focus({ preventScroll: true });
        let parent = parentEditors.get(project.id);
        if (!parent) {
            parent = createParentSection({ project, signal: parentController.signal,
                isCurrent: () => validSelection(project.id),
                onSelect: id => selectProject(id), onBrowse: browseChildren,
                onApplied: async ({ checked = false } = {}) => {
                    if (dead) return;
                    try { await onChanged?.(); } catch (error) { if (validSelection(project.id)) q('[data-pm-action-status]').textContent = `Parent ${checked ? 'checked' : 'saved'}. Sidebar refresh unavailable: ${message(error)}`; }
                    if (validSelection(project.id) && !checked) await selectProject(project.id);
                    if (isHere()) await loadCatalog();
                },
            });
            parentEditors.set(project.id, parent);
        }
        parent.updateProject(project);
        detail.append(parent.root);
        const folder = el('section', 'pm-section'); folder.append(el('h3', '', 'Starting folder'), el('p', 'pm-path', project.working_dir || 'No starting folder configured'));
        detail.append(folder);
        const resources = el('section', 'pm-section pm-resources'); resources.append(el('h3', '', 'Resources'), el('p', 'pm-note', MANAGED_PROJECT_IDS.includes(project.id) ? 'Working context, not an access boundary.' : 'Working context, not an access boundary. Detaching removes the association, not files or the starting folder.'));
        if (!project.resources.length) resources.append(el('p', 'pm-empty', 'No resources associated. A project can be conversation only.'));
        for (const resource of project.resources) resources.append(resourceRow(project, resource, life));
        if (['active', 'closed'].includes(life) && !MANAGED_PROJECT_IDS.includes(project.id)) resources.append(resourceForm(project));
        else if (MANAGED_PROJECT_IDS.includes(project.id)) resources.append(el('p', 'pm-note', 'Viper manages these associations.'));
        detail.append(resources);
        const history = el('details', 'pm-history-section'); const summary = el('summary', '', 'View history'); history.append(summary);
        const feed = el('div', 'pm-history'); feed.dataset.pmHistory = ''; history.append(feed); detail.append(history);
        let loaded = false;
        history.addEventListener('toggle', () => { if (history.open && !loaded) { loaded = true; void loadHistory(project, feed); } });
    }
    function contentSection(project) {
        let entry = contentEditors.get(project.id);
        if (!entry) {
            entry = { id: project.id, current: projectContent(project), base: null, editing: false, busy: false, checking: false, unknown: false, conflict: false, latestKnown: true, status: '' };
            entry.node = el('section', 'pm-section pm-content'); entry.node.dataset.pmContent = '';
            contentEditors.set(project.id, entry);
        }
        entry.current = projectContent(project); entry.life = projectLifecycle(project); entry.latestKnown = true;
        if (entry.needsRead) entry.status = '';
        entry.needsRead = false;
        if (entry.editing) entry.conflict = !sameContent(entry.base, entry.current);
        paintContent(entry);
        return entry.node;
    }
    function authored(tag, className, text) { const node = el(tag, className, text); node.dataset.i18nAuthored = ''; return node; }
    function savedContent(content, heading = '') {
        const block = el('div', 'pm-saved-content');
        if (heading) block.append(el('h4', '', heading));
        block.append(el('h4', '', 'Description'), authored('p', 'pm-content-text', content.description || 'No description yet.'), el('h4', '', 'Notes'), authored('p', 'pm-content-text pm-notes-text', content.notes || 'No notes yet.'));
        return block;
    }
    function paintContent(entry) {
        const host = entry.node, editable = ['active', 'closed'].includes(entry.life);
        if (!entry.editing) {
            host.replaceChildren();
            const heading = el('div', 'pm-content-heading'); heading.append(el('h3', '', 'About this project'));
            if (editable) { const edit = button('Edit', () => { entry.editing = true; entry.base = { ...entry.current }; entry.status = ''; entry.conflict = false; entry.unknown = false; entry.reviewed = false; paintContent(entry); entry.description.focus(); }); edit.dataset.pmContentEdit = ''; edit.disabled = !!entry.needsRead; heading.append(edit); }
            host.append(heading, savedContent(entry.current));
            if (entry.status) { const status = el('p', 'pm-note', entry.status); status.setAttribute('role', 'status'); host.append(status); }
            return;
        }
        if (!entry.form) {
            entry.originalContent = { ...entry.base };
            const form = el('form', 'pm-content-form'); form.dataset.pmContentForm = '';
            form.append(el('h3', '', 'About this project'), el('p', 'pm-note', 'Your description and notes — not live status. Both are optional.'));
            const field = (labelText, name, multiline) => {
                const label = el('label', '', labelText), input = el(multiline ? 'textarea' : 'input', 'ui-control');
                input.name = name; input.dataset.i18nAuthored = ''; input.dataset[name === 'description' ? 'pmDescription' : 'pmNotes'] = '';
                if (multiline) input.rows = 7; else input.type = 'text';
                input.value = entry.base[name];
                input.addEventListener('input', () => { if (!entry.busy) entry.status = ''; updateContentControls(entry); });
                label.append(input); form.append(label); return input;
            };
            entry.description = field('Description (optional)', 'description', false); entry.notes = field('Notes (optional)', 'notes', true);
            entry.initialValues = { description: entry.description.value, notes: entry.notes.value };
            const actions = el('div', 'pm-content-actions');
            entry.save = button('Save', () => void saveContent(entry), true); entry.save.dataset.pmContentSave = '';
            entry.cancel = button('Cancel', () => {
                if (entry.busy || entry.checking) return;
                entry.needsRead = entry.unknown; entry.editing = false; entry.form = null;
                entry.status = entry.needsRead ? 'Save outcome was not confirmed. Reading current saved text…' : '';
                entry.unknown = false; entry.conflict = false; paintContent(entry);
                entry.node.querySelector('[data-pm-content-edit]')?.focus();
                if (entry.needsRead && validSelection(entry.id)) void selectProject(entry.id);
            }); entry.cancel.dataset.pmContentCancel = '';
            entry.check = button('Check current saved text', () => void checkContent(entry)); entry.check.dataset.pmContentCheck = '';
            entry.continue = button('Continue with latest version', () => {
                entry.base = { ...entry.current }; entry.conflict = false;
                entry.status = 'Your draft is unchanged. The next Save replaces both saved fields with this draft, including fields you did not edit. Review the comparison first; the server will check that version again.';
                entry.reviewed = true; updateContentControls(entry); entry.notes.focus({ preventScroll: true });
            }); entry.continue.dataset.pmContentContinue = '';
            actions.append(entry.save, entry.cancel, entry.check, entry.continue);
            entry.feedback = el('p', 'pm-content-feedback'); entry.feedback.setAttribute('role', 'status');
            entry.comparison = el('div', 'pm-content-comparison'); entry.comparison.dataset.pmContentComparison = '';
            form.append(actions, entry.feedback, entry.comparison);
            form.addEventListener('submit', event => event.preventDefault()); // Enter in notes is a line break, never a save.
            host.replaceChildren(form); entry.form = form;
        }
        updateContentControls(entry);
    }
    function draftContent(entry) {
        // Textareas normalize CRLF/bare CR to LF. Preserve exact saved bytes for
        // each untouched field; changing the other field must not rewrite them.
        return Object.fromEntries(['description', 'notes'].map(name => [name, entry[name].value === entry.initialValues[name] ? entry.originalContent[name] : entry[name].value]));
    }
    function updateContentControls(entry) {
        if (!entry.editing) return;
        const pending = entry.busy || entry.checking, editable = ['active', 'closed'].includes(entry.life);
        const draft = draftContent(entry);
        const validation = Array.from(draft.description).length > 500 ? 'Description is limited to 500 characters. Your text is kept.'
            : /[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/.test(draft.description) ? 'Description must be a single line. Your text is kept.'
            : Array.from(draft.notes).length > 20000 ? 'Notes are limited to 20,000 characters. Your text is kept.' : '';
        entry.description.readOnly = entry.notes.readOnly = entry.busy;
        entry.save.disabled = pending || entry.unknown || entry.conflict || !editable || !!validation || sameContent(draft, entry.base);
        entry.save.textContent = entry.busy ? 'Saving…' : 'Save'; entry.cancel.disabled = pending;
        entry.check.hidden = !entry.unknown && !entry.conflict; entry.check.disabled = pending;
        entry.continue.hidden = !entry.conflict || entry.unknown; entry.continue.disabled = pending || !entry.latestKnown;
        entry.feedback.textContent = validation || entry.status || (entry.conflict ? 'Saved text changed elsewhere. Your draft is kept. Both fields, including fields you did not edit, will replace the saved text on the next Save. Compare below, then Continue with latest version to keep editing. Save checks that version again.' : !editable ? 'This project cannot be edited in its current state. Your draft is kept.' : '');
        entry.comparison.replaceChildren();
        if (entry.conflict || entry.unknown || entry.reviewed) entry.comparison.append(savedContent(entry.current, entry.latestKnown ? 'Current saved version' : 'Last read saved version — check current text first'));
    }
    async function checkContent(entry) {
        if (entry.busy || entry.checking || dead) return;
        entry.checking = true; entry.status = 'Reading current saved text…'; updateContentControls(entry);
        const controller = new AbortController(); contentControllers.add(controller);
        try {
            const data = await fetchJson(projectPrefix(entry.id) + 'detail', requestOptions(controller));
            if (dead) return;
            if (data?.project?.id !== entry.id) throw Error('Project detail response is incomplete.');
            entry.current = projectContent(data.project); entry.life = projectLifecycle(data.project);
            entry.latestKnown = true;
            entry.conflict = !sameContent(entry.base, entry.current);
            if (entry.unknown && sameContent(draftContent(entry), entry.current)) {
                entry.base = { ...entry.current }; entry.conflict = false;
                entry.status = 'Current saved text matches your draft.';
            } else entry.status = entry.conflict ? '' : 'Current saved text checked. Your draft is kept; Save is an explicit retry.';
            entry.unknown = false;
        } catch (error) { if (!dead) entry.status = `Current saved text unavailable: ${message(error)} Your draft is kept.`; }
        finally { contentControllers.delete(controller); entry.checking = false; if (!dead) updateContentControls(entry); }
    }
    async function saveContent(entry) {
        if (entry.save.disabled || dead) return;
        const draft = draftContent(entry); entry.busy = true; entry.status = 'Saving…'; updateContentControls(entry);
        const controller = new AbortController(); contentControllers.add(controller);
        try {
            const data = await saveProjectContent(entry.id, draft, entry.base, AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]));
            if (dead) return;
            if (data?.project?.id !== entry.id || typeof data.project.description !== 'string' || typeof data.project.notes !== 'string' || !sameContent(data.project, draft)) throw Error('The saved text response could not be confirmed.');
            entry.current = projectContent(data.project); entry.life = projectLifecycle(data.project);
            entry.editing = false; entry.form = null; entry.status = 'Saved.'; entry.unknown = false; entry.conflict = false;
            paintContent(entry);
            if (validSelection(entry.id)) {
                // Content replies are registry views, not observed detail resources.
                selected = { ...selected, ...entry.current, ...(data.project.lifecycle ? { lifecycle: data.project.lifecycle } : {}) };
                entry.node.querySelector('[data-pm-content-edit]')?.focus({ preventScroll: true });
                // Invalidate any detail snapshot begun before this confirmed write.
                void selectProject(entry.id);
            }
            if (isHere()) void loadCatalog();
        } catch (error) {
            if (dead) return;
            const current = error.body?.current_content;
            if (error.status === 409 && error.body?.reason_code === 'project_content_conflict') {
                entry.latestKnown = typeof current?.description === 'string' && typeof current?.notes === 'string';
                if (entry.latestKnown) entry.current = projectContent(current);
                entry.conflict = true; entry.status = '';
            } else {
                entry.unknown = !error.status || error.status >= 500;
                entry.status = entry.unknown ? `Save was not confirmed: ${message(error)} Your draft is kept. Check current saved text before any retry.` : `Not saved: ${message(error)} Your draft is kept.`;
            }
        } finally { contentControllers.delete(controller); entry.busy = false; if (!dead && entry.editing) updateContentControls(entry); }
    }
    function resourceRow(project, resource, life) {
        const row = el('div', 'pm-resource'); const observation = resource.observation;
        const head = el('div', 'pm-resource-head'); head.append(el('strong', '', resource.label || (resource.id === 'default' ? 'Initial folder association' : 'Resource')));
        if (['active', 'closed'].includes(life) && !MANAGED_PROJECT_IDS.includes(project.id)) {
            const detach = button('Detach', async () => {
                const ok = await openConfirmDialog({ title: 'Detach resource', body: `Remove the association with “${resource.path}”? Files and the starting folder are unchanged.`, confirmLabel: 'Detach' });
                if (ok === true) await changeProject(project, `/resources/${encodeURIComponent(resource.id)}/detach`, {}, detach, 'Detaching…');
            }); detach.setAttribute('aria-label', `Detach ${resource.label || resource.path}`); head.append(detach);
        }
        row.append(head, el('p', 'pm-path', resource.path));
        if (!observation) row.append(el('p', 'pm-note', 'Observation unavailable'));
        else {
            const words = { available: observation.kind === 'file' ? 'File available' : observation.kind === 'directory' ? 'Folder available' : 'Available', missing: 'Missing from this environment', unavailable: 'Inspection unavailable' };
            row.append(el('p', 'pm-resource-status', words[observation.status] || 'Observation unavailable'));
            const git = observation.git;
            if (git?.status === 'repository') {
                row.append(el('p', 'pm-git', git.branch ? `Branch · ${git.branch}` : git.head ? 'Detached HEAD' : 'Branch unavailable'));
                if (git.head) row.append(el('p', 'pm-git', `Commit · ${git.head}`));
                if (git.root && git.root !== resource.path) row.append(el('p', 'pm-path', `Repository · ${git.root}`));
            } else if (git?.status === 'not_repository') row.append(el('p', 'pm-note', 'Not a Git repository'));
            else if (observation.status === 'available') row.append(el('p', 'pm-note', 'Git observation unavailable'));
            if (observation.error || git?.error) row.append(el('p', 'pm-note', observation.error || git.error));
        }
        return row;
    }
    function resourceForm(project) {
        const form = el('form', 'pm-resource-form');
        form.innerHTML = '<label>Resource path<input class="ui-control" name="path" required placeholder="/path/visible/in/this/environment" data-pm-resource-path></label><label>Label <span>(optional)</span><input class="ui-control" name="label" maxlength="160" data-pm-resource-label></label><button class="btn btn-default" type="submit">Add resource</button><p class="pm-note">Associate an existing path. This does not create, clone or move files.</p>';
        form.addEventListener('submit', event => { event.preventDefault(); const path = form.elements.path.value.trim(), label = form.elements.label.value.trim(); if (path) void changeProject(project, '/resources', { path, ...(label ? { label } : {}) }, form.querySelector('button'), 'Adding…'); });
        return form;
    }
    function addFile(row, host) {
        const { source, reader: copy } = deliveredFile(row), filename = row.filename || 'Delivered file';
        const candidateKind = copy ? documentReaderKind(filename, row.mime) : '';
        const kind = candidateKind === 'image' && !source ? '' : candidateKind;
        if (kind) {
            const read = button(filename, () => reader.open({ filename, kind, reader: copy, src: source, url: source, canOpen: false }, { owner: host, returnFocus: read })); read.classList.add('pm-document'); host.append(read);
        }
        if (source) { const a = el('a', 'pm-download', kind ? 'Download' : filename); a.href = source; a.download = filename; host.append(a); }
        if (!kind && !source) host.append(el('p', 'pm-note', `${filename} · delivered copy unavailable`));
    }
    async function readResult(id, host, project) {
        host.setAttribute('aria-busy', 'true');
        host.replaceChildren(el('p', 'pm-note', 'Loading result…'));
        const current = detailRequest;
        try {
            const data = await fetchJson(`/api/tasks/${encodeURIComponent(id)}`, requestOptions(detailController));
            if (!validSelection(project.id) || current !== detailRequest || !host.isConnected) return;
            host.replaceChildren(rich(data.result || 'No result text is available.'));
            for (const artifact of data.artifacts || []) {
                const url = taskArtifactDownloadUrl(id, artifact.name, artifact.relpath || '');
                if (url) addFile({ filename: artifact.name, mime: '', download_url: url, task_id: id }, host);
                else if ([artifact.name, artifact.relpath].some(value => typeof value === 'string' && !value.isWellFormed())) {
                    host.append(el('p', 'pm-note', `Retained file cannot be directly downloaded: ${JSON.stringify(artifact.relpath || artifact.name)}`));
                }
            }
        } catch (error) { if (validSelection(project.id) && current === detailRequest) errorIn(host, `Result unavailable: ${message(error)}`, () => readResult(id, host, project)); }
        finally { host.removeAttribute('aria-busy'); }
    }
    function historyRow(row, project) {
        if (['quiz_answer', 'quiz_closed', 'task_evidence'].includes(row.system_type) || row.summary_kind && row.historical_terminal) return null;
        const item = el('article', 'pm-history-row');
        item.append(el('p', 'pm-history-meta', [row.sender_label || (row.role === 'user' ? 'You' : row.role === 'assistant' ? 'Viper' : 'System'), row.ts].filter(Boolean).join(' · ')));
        if (row.is_progress) { const progress = el('details', 'pm-progress'); progress.append(el('summary', '', 'Progress entry'), rich(row.text || row.content)); item.append(progress); }
        else if (row.text || row.content) item.append(rich(row.text || row.content));
        if (row.filename || row.msg_type === 'document') addFile(row, item);
        for (const attachment of attachmentViews(row.attachments)) addFile({ filename: attachment.name, mime: attachment.mime, download_url: attachment.url }, item);
        if (row.caption) item.append(el('p', '', row.caption));
        if (row.task_id && !row.is_progress) {
            const result = el('details', 'pm-result'), summary = el('summary', '', 'Read result'); const body = el('div', 'pm-result-body'); result.append(summary, body); let loaded = false;
            result.addEventListener('toggle', () => { if (result.open && !loaded) { loaded = true; void readResult(row.task_id, body, project); } }); item.append(result);
        }
        return item;
    }
    async function loadHistory(project, feed, older = false) {
        if (!validSelection(project.id) || historyBusy) return;
        historyBusy = true; historyInterrupted = false; historyController?.abort(); historyController = new AbortController(); const current = detailRequest;
        const pending = el('p', 'pm-note', 'Loading history…'); feed.prepend(pending);
        try {
            const data = await apiClient.chatHistory({ chatId: project.chat_id, cursor: older ? historyCursor : null, signal: historyController.signal });
            if (!validSelection(project.id) || current !== detailRequest) return;
            if (data.error || !Array.isArray(data.messages)) throw Error(data.error || 'History response is incomplete.');
            feed.querySelector('[data-pm-older]')?.remove(); feed.querySelector('.pm-history-gap')?.remove();
            const fragment = document.createDocumentFragment();
            for (const row of data.messages) {
                const key = row.history_id || row.row_address;
                if (key && historySeen.has(key)) continue;
                const item = historyRow(row, project);
                if (item) { if (key) historySeen.add(key); fragment.append(item); }
            }
            if (!data.messages.length && !older) fragment.append(el('p', 'pm-empty', 'No messages in this retained history window.'));
            feed.prepend(fragment); historyCursor = data.next_cursor;
            if (data.window?.complete === false || data.window?.truncated_by?.length) feed.prepend(el('p', 'pm-history-gap pm-note', `This is a retained history window${data.window?.truncated_by?.length ? ' · ' + data.window.truncated_by.join(', ') : ''}.`));
            if (data.has_more) {
                if (historyCursor) { const more = button('Load older messages', () => loadHistory(project, feed, true)); more.dataset.pmOlder = ''; feed.prepend(more); }
                else feed.prepend(el('p', 'pm-history-gap pm-note', 'Older history exists but its paging cursor is unavailable.'));
            }
        } catch (error) {
            if (!validSelection(project.id) || current !== detailRequest || historyController.signal.aborted) return;
            const failure = el('div', 'pm-error'); failure.setAttribute('role', 'alert'); failure.append(el('p', '', `History unavailable: ${message(error)}`), button('Retry history', () => { failure.remove(); void loadHistory(project, feed, older); })); feed.prepend(failure);
        } finally { pending.remove(); if (current === detailRequest) historyBusy = false; }
    }
    const browse = el('nav', 'pm-browse'); browse.setAttribute('aria-label', 'Project organization');
    const scopeLabel = el('span', 'pm-note'); scopeLabel.dataset.pmScope = '';
    function browseChildren(id, name) { parentScope = id === null ? null : { id, name }; offset = 0; scopeLabel.textContent = parentScope ? (id ? `Children of ${name}` : 'Top-level projects') : 'All projects'; void loadCatalog(); }
    browse.append(button('All projects', () => browseChildren(null, '')), button('Top level', () => browseChildren('', 'Top level')), scopeLabel);
    q('.pm-filters').after(browse); scopeLabel.textContent = 'All projects';
    function applySearch(event) { event?.preventDefault(); search = q('[data-pm-search]').value.trim(); lifecycle = q('[data-pm-lifecycle]').value; offset = 0; void loadCatalog(); }
    root.querySelector('.pm-filters').addEventListener('submit', applySearch);
    q('[data-pm-lifecycle]').addEventListener('change', applySearch);
    q('[data-pm-search]').addEventListener('search', applySearch);
    q('[data-pm-refresh]').addEventListener('click', () => { void loadCatalog(); if (selectedId) void selectProject(selectedId); });
    q('[data-pm-previous]').addEventListener('click', () => { offset = Math.max(0, offset - pageSize); void loadCatalog(); });
    q('[data-pm-next]').addEventListener('click', () => { offset += pageSize; void loadCatalog(); });
    q('[data-pm-create]').addEventListener('click', () => onCreate?.());
    function exposure() {
        const next = state.activePage === 'dashboard' && state.dashboardActiveSubtab === 'projects';
        if (next === live) return; live = next;
        if (live) { void loadCatalog(); if (selectedId) void selectProject(selectedId); }
        else { ++catalogRequest; catalogController?.abort(); stopDetailReads(); }
    }
    window.addEventListener('ouro:page-shown', exposure);
    window.addEventListener('ouro:dashboard-subtab-shown', exposure);
    exposure();
    return { root, select: selectProject, refresh: () => loadCatalog(), destroy() { dead = true; live = false; ++catalogRequest; catalogController?.abort(); for (const controller of contentControllers) controller.abort(); contentEditors.clear(); parentController.abort(); parentEditors.clear(); stopDetailReads(); reader.destroy(); cleanRich(detail); window.removeEventListener('ouro:page-shown', exposure); window.removeEventListener('ouro:dashboard-subtab-shown', exposure); root.remove(); } };
}
