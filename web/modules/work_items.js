/** Project notebook: authored dispositions are never execution state. No scheduler or chat sends. */
import { fetchJson, fetchTaskDetailStrict } from './api_client.js';
import { createWorkItemCapture } from './work_item_capture.js';

export const DISPOSITIONS = Object.freeze({ planned: 'Planned', idea: 'Ideas', done: 'Done', set_aside: 'Set aside' });
export const boardRoute = (name, args = {}) => `/api/extensions/project-boards/${name}` + (Object.keys(args).length ? '?' + new URLSearchParams(args) : '');
export const authorLabel = author => author?.label || (author?.surface === 'agent' ? 'Ouroboros' : 'via Interface/API');
export const itemSource = (item, comment = null) => `Project: ${item.project_id}\nWork Item: ${item.id}${comment ? '\nComment: ' + comment.id : ''}\nSource: ${boardRoute('item', { item_id: item.id })}`;
export const itemText = (item, selected = '', comment = null) => [selected || (comment ? comment.text : [item.title, item.description, item.outcome && `Outcome\n${item.outcome}`].filter(Boolean).join('\n\n')), itemSource(item, comment)].join('\n\n');
export function containedSelection(host, doc = host.ownerDocument) { const selection = doc.getSelection(); return selection?.rangeCount === 1 && !selection.isCollapsed && host.contains(selection.anchorNode) && host.contains(selection.focusNode) ? selection.toString() : ''; }
export function safeReference(value) {
    try { const url = new URL(value, location.origin); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
}
export function fieldsOf(item) {
    return { title: item.title, description: item.description || '', outcome: item.outcome || '', disposition: item.disposition,
        archived: !!item.archived, references: structuredClone(item.references || []), prerequisites: (item.prerequisites || []).map(p => ({ item_id: p.item_id, needs: p.needs || '' })) };
}
export function freshOperation(name, payload) { return { name, payload: { ...structuredClone(payload), request_id: crypto.randomUUID() }, state: 'ready' }; }
export function workFact(data) {
    if (!data) return 'Linked work not found';
    const task = data.task || data;
    const axes = data.outcome_axes || task.outcome_axes;
    if (axes) return [['Lifecycle', axes.lifecycle?.status || task.status || 'unknown'], ['Execution', axes.execution?.status || 'unknown'], ['Objective', axes.objective?.status || 'unknown'], ['Review', axes.review?.status || 'unknown']].map(([label, fact]) => `${label}: ${fact}`).join(' · ');
    return [`Lifecycle: ${task.status || 'unknown'}`, 'Objective: unavailable', task.error || ''].filter(x => typeof x === 'string' && x).join(' · ');
}
const el = (doc, tag, cls = '', text = '') => { const node = doc.createElement(tag); node.className = cls; node.textContent = text; return node; };
const when = value => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? 'Time unavailable' : date.toLocaleString(); };

export function createWorkItems({ sendToChat, openReference, onCaptured, read = fetchJson, taskRead = fetchTaskDetailStrict, doc = document } = {}) {
    // Viewer: Elias sees this project's remaining work and captures or discusses an item without steering a run.
    const root = el(doc, 'section', 'work-items'); root.hidden = true; root.setAttribute('aria-label', 'Project Work Items');
    root.innerHTML = `<header class="wi-header"><div><h2>Work Items</h2><p data-wi-project></p></div><div class="wi-actions"><button type="button" class="btn btn-default" data-wi-back>Back to list</button><button type="button" class="btn btn-default" data-wi-close aria-label="Close Work Items">Close</button></div></header>
      <div class="wi-body"><section class="wi-index" aria-label="Work Items list"><form class="wi-add"><label>Add item<input class="ui-control" name="title" placeholder="A thought to keep…" required autocomplete="off"></label><button class="btn btn-primary" type="submit">Add item</button></form><div class="wi-capture-status" role="status"></div>
      <form class="wi-search" role="search"><label>Find an item<input type="search" class="ui-control" name="q" placeholder="Search items and discussion"></label><button type="submit" class="btn btn-default">Search</button></form>
      <div class="wi-categories" role="group" aria-label="Work Item categories">${Object.entries(DISPOSITIONS).map(([key, label]) => `<button type="button" class="btn btn-default wi-category" data-wi-filter="${key}" aria-pressed="false"><span>${label}</span><span data-wi-count="${key}">—</span></button>`).join('')}<button type="button" class="btn btn-primary wi-categories-all" data-wi-filter="" aria-pressed="true">All items</button></div>
      <div class="wi-index-tools"><label><input type="checkbox" data-wi-archived> Archived items</label><button type="button" class="btn btn-default" data-wi-refresh>Refresh</button><a class="wi-export" data-wi-export>Export JSON</a></div><p class="wi-list-status" role="status"></p><div class="wi-list"></div><nav class="wi-pages" aria-label="Work Items pages"><button type="button" class="btn btn-default" data-wi-prev>Previous</button><span data-wi-page></span><button type="button" class="btn btn-default" data-wi-next>Next</button></nav></section>
      <section class="wi-detail" aria-label="Work Item detail"><p class="wi-empty">Choose an item to read or discuss.</p></section></div>`;
    doc.body.append(root);
    const q = sel => root.querySelector(sel), boards = new Map(), drafts = new Map(), attachments = new Map();
    const lifetime = new AbortController(), mutations = new Set();
    let returnFocus = null, board = null, dead = false, listSequence = 0, detailSequence = 0, listController = null, detailController = null, detailItem = null;
    const live = () => !dead && !root.hidden;
    const currentItem = (b, id) => live() && board === b && b.selected === id;
    function button(text, fn, cls = 'btn btn-default') { const n = el(doc, 'button', cls, text); n.type = 'button'; n.onclick = fn; return n; }
    function note(host, text, failure = false) { host.textContent = text; host.classList.toggle('wi-error', failure); }
    function stateFor(project) {
        let b = boards.get(project.id);
        if (!b) { b = { project, selected: '', drafts: new Map(), capture: '', captureOp: null, captureMessage: '', query: '', filter: '', archived: false, offset: 0, total: 0, limit: 100, more: false, items: [], counts: {} }; boards.set(project.id, b); }
        b.project = project; return b;
    }
    function editorFor(b, item) {
        let d = drafts.get(item.id);
        if (!d) { d = { base: structuredClone(item), fields: fieldsOf(item), comment: '', quote: null, operation: null, commentOp: null, moveOp: null, moveNotice: '', conflict: null, notice: '', commentNotice: '', editing: false }; drafts.set(item.id, d); }
        // Refresh may advance evidence, never rewrite an open/dirty editor or an unresolved mutation.
        if (!d.editing && !d.operation && !d.moveOp) { d.base = structuredClone(item); d.fields = fieldsOf(item); }
        return d;
    }
    function cancelReads() { ++listSequence; ++detailSequence; listController?.abort(); detailController?.abort(); }
    function close({ restoreFocus = true } = {}) { root.hidden = true; cancelReads(); if (restoreFocus && returnFocus?.isConnected && !dead) returnFocus.focus({ preventScroll: true }); }
    async function request(name, args, controller) {
        const data = await read(boardRoute(name, args), { cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(25000)]) }, { rejectOkFalse: true });
        if (data?.ok !== true) throw Error('Work Items response is incomplete.'); return data;
    }
    async function mutate(op) {
        const wasUnknown = op.state === 'unknown'; op.state = 'sending'; const controller = new AbortController(); mutations.add(controller);
        try {
            const data = await read(boardRoute(op.name), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(op.payload), signal: AbortSignal.any([controller.signal, AbortSignal.timeout(25000)]) }, { rejectOkFalse: true });
            if (data?.ok !== true || !data.item?.id || !Number.isInteger(data.item.revision)) throw Error('The saved response is incomplete.');
            op.state = 'confirmed'; return data;
        } catch (error) {
            // The store checks an existing request receipt before comparing revisions.
            // Only its typed current-item revision conflict can settle an unknown update.
            const revisionRefusal = op.name === 'update' && error.status === 409 && error.body?.error?.code === 'revision_conflict' && error.body?.current?.id === op.payload.item_id;
            const boardRejection = Number.isInteger(error.status) && error.status >= 400 && error.status < 500 && error.body?.ok === false && (wasUnknown ? revisionRefusal : ['invalid', 'missing', 'readonly', 'revision_conflict', 'request_conflict'].includes(error.body?.error?.code));
            op.state = boardRejection ? 'rejected' : 'unknown'; throw error;
        }
        finally { mutations.delete(controller); }
    }
    function paintCapture() {
        if (!board) return;
        const b = board, form = q('.wi-add'), status = q('.wi-capture-status');
        form.elements.title.value = b.capture; form.elements.title.disabled = !!b.captureOp;
        form.querySelector('button').disabled = !!b.captureOp;
        status.replaceChildren(el(doc, 'span', '', b.captureMessage));
        if (b.captureOp?.state === 'unknown') status.append(button('Retry same request', () => capture(b)));
    }
    async function capture(b) {
        if (b.captureOp?.state === 'sending') return;
        if (!b.captureOp) { if (!b.capture.trim()) return; b.captureOp = freshOperation('capture', { project_id: b.project.id, title: b.capture }); b.captureOp.selection = b.selected; b.captureOp.detailSequence = detailSequence; }
        b.captureMessage = 'Saving item…'; if (board === b) paintCapture();
        try {
            const operation = b.captureOp, data = await mutate(operation); if (dead) return;
            b.captureOp = null; b.capture = ''; b.captureMessage = 'Item saved as Idea.';
            if (board === b && live()) { paintCapture(); await loadList(); if (b.selected === operation.selection && detailSequence === operation.detailSequence) void openItem(data.item.id); }
        } catch (error) {
            if (dead) return;
            b.captureMessage = b.captureOp.state === 'unknown' ? 'Save not confirmed. Your title and request are kept; retry uses the same key.' : error.message;
            if (b.captureOp.state === 'rejected') b.captureOp = null;
            if (board === b && live()) paintCapture();
        }
    }
    function paintCategories() {
        const b = board;
        // These nodes survive list reads, preserving the focused category control.
        root.querySelectorAll('[data-wi-filter]').forEach(node => {
            const selected = node.dataset.wiFilter === b.filter;
            node.setAttribute('aria-pressed', String(selected));
            node.classList.toggle('btn-primary', selected);
            node.classList.toggle('btn-default', !selected);
        });
        for (const key of Object.keys(DISPOSITIONS)) {
            const count = b.counts[key], node = q(`[data-wi-count="${key}"]`);
            node.textContent = Number.isInteger(count) && count >= 0 ? String(count) : '—';
            node.parentElement.title = 'Items matching this search and archive selection, before category filtering.';
        }
    }
    function paintList() {
        const b = board, host = q('.wi-list'); host.replaceChildren();
        paintCategories();
        host.setAttribute('aria-label', b.filter ? `${DISPOSITIONS[b.filter]} Work Items` : 'All Work Items');
        const items = b.items.filter(item => !b.filter || item.disposition === b.filter);
        for (const item of items) {
            const row = button('', () => openItem(item.id), 'wi-row'); row.dataset.wiItem = item.id; row.setAttribute('aria-pressed', String(b.selected === item.id));
            row.append(el(doc, 'strong', '', item.title), el(doc, 'span', 'wi-preview', item.preview || item.description || ''));
            const prerequisites = item.prerequisites || [];
            row.append(el(doc, 'span', 'wi-meta', `${item.comment_count || 0} comments · ${when(item.updated_at)}${prerequisites.length ? ` · ${prerequisites.length} prerequisite${prerequisites.length === 1 ? '' : 's'}` : ''}${item.references?.length ? ` · ${item.references.length} work/reference links` : ''}`));
            host.append(row);
        }
        if (!items.length) host.append(el(doc, 'p', 'wi-empty', b.query || b.filter || b.archived ? 'No matching items.' : 'No items yet.'));
        q('[data-wi-page]').textContent = b.total ? `${b.offset + 1}–${b.offset + b.items.length} of ${b.total}` : '0 items';
        q('[data-wi-prev]').disabled = b.offset === 0; q('[data-wi-next]').disabled = !b.more;
    }
    async function loadList() {
        if (!live() || !board) return;
        const b = board; listController?.abort(); listController = new AbortController(); const sequence = ++listSequence;
        b.counts = {}; paintCategories();
        q('.wi-list').setAttribute('aria-busy', 'true');
        q('.wi-list').replaceChildren();
        q('[data-wi-page]').textContent = '';
        q('[data-wi-prev]').disabled = true; q('[data-wi-next]').disabled = true;
        note(q('.wi-list-status'), 'Loading items…');
        try {
            const args = { project_id: b.project.id, archived: String(b.archived), offset: String(b.offset), limit: String(b.limit) };
            if (b.query) args.q = b.query; if (b.filter) args.disposition = b.filter;
            const data = await request('list', args, listController);
            if (!live() || board !== b || sequence !== listSequence) return;
            if (!Array.isArray(data.items) || !Number.isInteger(data.total) || !Number.isInteger(data.offset) || typeof data.has_more !== 'boolean') throw Error('Work Items list is incomplete.');
            b.items = data.items; b.counts = data.counts || {}; b.total = data.total; b.offset = data.offset; b.more = data.has_more; b.limit = data.limit || 100;
            paintList(); note(q('.wi-list-status'), '');
        } catch (error) {
            if (!live() || board !== b || sequence !== listSequence || listController.signal.aborted) return;
            note(q('.wi-list-status'), `Work Items unavailable: ${error.message} The skill may be disabled or unavailable.`, true);
        } finally { if (live() && board === b && sequence === listSequence) q('.wi-list').removeAttribute('aria-busy'); }
    }
    function multiline(label, text) { const wrap = el(doc, 'section', 'wi-section'); wrap.append(el(doc, 'h4', '', label), el(doc, 'p', 'wi-text', text || '—')); return wrap; }
    function field(label, value, name, multiline = false) {
        const wrap = el(doc, 'label', 'wi-field', label), input = el(doc, multiline ? 'textarea' : 'input', 'ui-control'); input.name = name; input.value = value; if (multiline) input.rows = 4; wrap.append(input); return { wrap, input };
    }
    function linkEditor(d, form) {
        const links = el(doc, 'details', 'wi-link-editor'); links.append(el(doc, 'summary', '', 'Work links and prerequisites'));
        const referenceRows = el(doc, 'div'), prerequisiteRows = el(doc, 'div');
        function redraw() {
            referenceRows.replaceChildren(); prerequisiteRows.replaceChildren();
            d.fields.references.forEach((ref, index) => {
                const row = el(doc, 'div', 'wi-reference-edit'), kind = el(doc, 'select', 'ui-control'); kind.setAttribute('aria-label', 'Reference kind');
                for (const name of ['task', 'row', 'url']) { const o = el(doc, 'option', '', name); o.value = name; kind.append(o); } kind.value = ref.kind;
                const value = field('Reference', ref.value, `reference-${index}`), label = field('Label', ref.label || '', `reference-label-${index}`);
                kind.onchange = () => { ref.kind = kind.value; }; value.input.oninput = () => { ref.value = value.input.value; }; label.input.oninput = () => { ref.label = label.input.value; };
                row.append(kind, value.wrap, label.wrap, button('Remove', () => { d.fields.references.splice(index, 1); redraw(); })); referenceRows.append(row);
            });
            d.fields.prerequisites.forEach((p, index) => {
                const row = el(doc, 'div', 'wi-prerequisite-edit'), id = field('Item ID', p.item_id, `prerequisite-${index}`), needs = field('Required outcome', p.needs || '', `needs-${index}`);
                id.input.oninput = () => { p.item_id = id.input.value; }; needs.input.oninput = () => { p.needs = needs.input.value; };
                row.append(id.wrap, needs.wrap, button('Remove', () => { d.fields.prerequisites.splice(index, 1); redraw(); })); prerequisiteRows.append(row);
            });
        }
        links.append(referenceRows, button('Add work/reference link', () => { d.fields.references.push({ kind: 'task', value: '', label: '' }); redraw(); }), prerequisiteRows,
            button('Add prerequisite', () => { d.fields.prerequisites.push({ item_id: '', needs: '' }); redraw(); })); redraw(); form.append(links);
    }
    async function save(b, d, againstCurrent = false) {
        if (d.moveOp) return;
        if (d.operation?.state === 'sending') return;
        if (!d.operation) d.operation = freshOperation('update', { item_id: d.base.id, expected_revision: againstCurrent ? d.conflict.revision : d.base.revision, changes: d.fields });
        d.notice = 'Saving…'; paintDetail(b, d);
        try {
            const data = await mutate(d.operation); if (dead) return;
            d.operation = null; d.conflict = null; d.base = data.item; d.fields = fieldsOf(data.item); d.editing = false; d.notice = 'Saved.';
            if (currentItem(b, d.base.id)) { await openItem(d.base.id); void loadList(); }
        } catch (error) {
            if (dead) return;
            const rejected = d.operation.state === 'rejected';
            d.notice = rejected ? error.message : 'Save not confirmed. Your draft and exact request are kept. Check current saved text or retry the same request.';
            if (rejected) { d.operation = null; if (error.status === 409 && error.body?.current?.id === d.base.id) d.conflict = error.body.current; }
            if (currentItem(b, d.base.id)) paintDetail(b, d);
        }
    }
    async function addComment(b, d) {
        if (d.moveOp) return;
        if (d.commentOp?.state === 'sending') return;
        if (!d.commentOp) { if (!d.comment.trim()) return; d.commentOp = freshOperation('comment', { item_id: d.base.id, text: d.comment, quote_comment_id: d.quote?.id || null }); }
        d.commentNotice = 'Posting…'; paintDetail(b, d);
        try {
            const data = await mutate(d.commentOp); if (dead) return;
            if (JSON.stringify(fieldsOf(data.item)) === JSON.stringify(fieldsOf(d.base))) d.base = data.item;
            d.commentOp = null; d.comment = ''; d.quote = null; d.commentNotice = 'Comment saved.';
            if (currentItem(b, d.base.id)) { await openItem(d.base.id); void loadList(); }
        } catch (error) {
            if (dead) return;
            d.commentNotice = d.commentOp.state === 'rejected' ? error.message : 'Comment not confirmed. Text and request are kept; retry uses the same key.';
            if (d.commentOp.state === 'rejected') d.commentOp = null;
            if (currentItem(b, d.base.id)) paintDetail(b, d);
        }
    }
    async function bring(b, text, feedback) {
        const sequence = detailSequence, id = b.selected;
        const current = () => currentItem(b, id) && sequence === detailSequence;
        try { if (!sendToChat) throw Error('Originating-room chat transfer is unavailable.'); const added = await sendToChat(b.project.id, text, { current, signal: detailController?.signal }); if (current()) { if (added !== true) throw Error('The room changed before text was added. Nothing was added.'); close({ restoreFocus: false }); } }
        catch (error) { if (live() && board === b) note(feedback, error.message, true); }
    }
    function followItem(b, item) {
        b.selected = '';
        board = stateFor(boards.get(item.project_id)?.project || { id: item.project_id, name: item.project_id });
        board.filter = item.disposition; board.offset = 0; board.archived = !!item.archived; board.query = '';
        detailItem = null; syncBoard(); void loadList(); void openItem(item.id);
    }
    async function moveItem(b, d, destination) {
        if (d.moveOp?.state === 'sending' || d.operation || d.commentOp) return;
        if (!d.moveOp) d.moveOp = freshOperation('update', { item_id: d.base.id, expected_revision: d.moveConflict?.revision || detailItem?.item?.revision || d.base.revision, changes: { project_id: destination } });
        d.moveNotice = 'Moving item…'; paintDetail(b, d);
        try {
            const data = await mutate(d.moveOp); if (dead) return;
            d.moveOp = null; d.moveConflict = null;
            // A location-only move must not rebase an unsaved whole-item edit.
            if (!d.editing) { d.base = data.item; d.fields = fieldsOf(data.item); }
            d.moveNotice = 'Move saved. Discussion, links and drafts are kept.';
            // A duplicate receipt is historical; resolve the item's current home.
            if (currentItem(b, d.base.id)) {
                try {
                    const current = await request('item', { item_id: d.base.id }, detailController);
                    if (current.item?.id !== d.base.id || typeof current.item.project_id !== 'string') throw Error('Current location response is incomplete.');
                    if (currentItem(b, d.base.id)) followItem(b, current.item);
                } catch (error) {
                    d.moveNotice = `Move saved. Current location could not be refreshed: ${error.message}`;
                    if (currentItem(b, d.base.id)) paintDetail(b, d);
                }
            }
        } catch (error) {
            if (dead) return;
            const rejected = d.moveOp?.state === 'rejected';
            d.moveNotice = d.moveOp && !rejected ? 'Move not confirmed. The exact request is kept; check the current item or retry the same request.' : error.message;
            if (rejected) {
                d.moveOp = null;
                if (error.body?.error?.code === 'revision_conflict' && error.body.current?.id === d.base.id) {
                    d.moveConflict = error.body.current;
                    d.moveNotice = `Item changed; current project is ${d.moveConflict.project_id}, revision ${d.moveConflict.revision}. Your drafts are kept. Choose a destination again to move this current revision without replacing its text.`;
                }
            }
            if (currentItem(b, d.base.id)) paintDetail(b, d);
        }
    }
    function moveChooser(b, d, host) {
        const section = el(doc, 'details', 'wi-move'), summary = el(doc, 'summary', '', 'Move to Project'); section.append(summary);
        const form = el(doc, 'form', 'wi-search'), search = field('Find a project', '', 'project-search');
        const submit = button('Search', () => {}); submit.type = 'submit'; form.append(search.wrap, submit);
        const choices = el(doc, 'div', 'wi-move-choices'), paging = el(doc, 'div', 'wi-actions');
        const status = el(doc, 'p', 'wi-feedback', d.moveNotice); status.setAttribute('role', 'status');
        let offset = 0, query = '', controller = null, sequence = 0;
        const previous = button('Previous projects', () => { offset = Math.max(0, offset - 50); void load(); });
        const next = button('Next projects', () => { offset += 50; void load(); }); paging.append(previous, next);
        const current = () => currentItem(b, d.base.id) && section.isConnected;
        async function load() {
            controller?.abort(); controller = new AbortController(); const own = ++sequence;
            choices.textContent = 'Loading projects…'; previous.disabled = next.disabled = true;
            try {
                const data = await read('/api/projects/catalog?' + new URLSearchParams({ lifecycle: 'all', q: query, offset: String(offset), limit: '50' }),
                    { cache: 'no-store', signal: AbortSignal.any([controller.signal, detailController.signal, AbortSignal.timeout(25000)]) }, { rejectOkFalse: true });
                if (!current() || own !== sequence) return;
                if (!Array.isArray(data.projects) || !Number.isInteger(data.offset) || typeof data.has_more !== 'boolean') throw Error('Project choices are incomplete.');
                choices.replaceChildren(); offset = data.offset;
                for (const project of data.projects) {
                    stateFor(project);
                    if (project.id === (detailItem?.item?.project_id || d.base.project_id)) continue;
                    const choose = button(`${project.name || project.id}${project.lifecycle === 'tombstoned' ? ' · Closed' : ''}`, () => void moveItem(b, d, project.id));
                    choose.dataset.wiMoveProject = project.id; choose.disabled = !!(d.moveOp || d.operation || d.commentOp); choices.append(choose);
                }
                if (!choices.childElementCount) choices.append(el(doc, 'p', 'wi-empty', 'No other projects on this page.'));
                previous.disabled = offset === 0; next.disabled = !data.has_more;
            } catch (error) { if (current() && own === sequence && !controller.signal.aborted) { choices.replaceChildren(button('Retry project choices', load)); note(status, error.message, true); } }
        }
        form.onsubmit = event => { event.preventDefault(); query = search.input.value; offset = 0; void load(); };
        section.ontoggle = () => { if (section.open && !choices.childElementCount) void load(); };
        section.append(form, choices, paging, status);
        section.open = !!(d.moveNotice || d.moveOp);
        if (d.moveNotice) section.append(button('Refresh current item', () => openItem(d.base.id)));
        if (d.moveOp?.state === 'unknown') { section.open = true; section.append(button('Check current item', () => openItem(d.base.id)), button('Retry same move', () => moveItem(b, d))); }
        host.append(section);
    }
    function paintDetail(b, d) {
        if (!currentItem(b, d.base.id)) return;
        const host = q('.wi-detail'), item = detailItem?.item?.id === d.base.id ? detailItem.item : d.base;
        host.replaceChildren();
        const top = el(doc, 'div', 'wi-detail-top'), identity = el(doc, 'div'); identity.append(el(doc, 'p', 'wi-meta', `${b.project.name || b.project.id} · ${item.disposition.replace('_', ' ')}`), el(doc, 'h3', '', item.title));
        top.append(identity, button(d.editing ? 'Cancel editing' : 'Edit item', () => { if (d.operation || d.moveOp) return; d.editing = !d.editing; if (!d.editing) { d.fields = fieldsOf(d.base); d.conflict = null; d.notice = ''; } paintDetail(b, d); })); host.append(top);
        const feedback = el(doc, 'p', 'wi-feedback'); feedback.setAttribute('role', 'status');
        function transferButton(label, action, comment = null, selectionHost = host) {
            const selectionText = () => { const selection = doc.getSelection(), thread = host.querySelector('.wi-thread'); return !comment && selection?.rangeCount && thread && selection.getRangeAt(0).intersectsNode(thread) ? '' : containedSelection(selectionHost, doc); };
            let selected = ''; const node = button(label, () => { const text = itemText(item, selected || selectionText(), comment); selected = ''; void action(text); });
            node.onpointerdown = () => { selected = selectionText(); }; node.onkeydown = event => { if (['Enter', ' '].includes(event.key)) selected = selectionText(); }; return node;
        }
        const commentShortcut = button('Add comment', () => {
            const input = host.querySelector('.wi-comment-form textarea');
            if (!input || input.disabled) return;
            input.scrollIntoView({ block: 'center', behavior: 'instant' });
            input.focus({ preventScroll: true });
        });
        commentShortcut.dataset.wiAddComment = '';
        commentShortcut.disabled = !!(d.commentOp || d.moveOp);
        const actions = el(doc, 'div', 'wi-actions'); actions.append(commentShortcut, transferButton('Bring to chat', text => bring(b, text, feedback)), transferButton('Copy item', async text => { try { await navigator.clipboard.writeText(text); note(feedback, 'Item copied.'); } catch { note(feedback, 'Copy is unavailable. Select the item text to copy.', true); } })); host.append(actions, feedback); moveChooser(b, d, host);
        const identityRow = el(doc, 'p', 'wi-meta wi-item-identity', `Item ID: ${item.id}`); identityRow.append(button('Copy item ID', async () => { try { await navigator.clipboard.writeText(item.id); note(feedback, 'Item ID copied.'); } catch { note(feedback, 'Copy is unavailable. Select the item ID.', true); } })); host.append(identityRow);
        if (d.editing) {
            const form = el(doc, 'form', 'wi-edit');
            for (const [key, label] of [['title', 'Title'], ['description', 'Description'], ['outcome', 'Outcome']]) {
                const f = field(label, d.fields[key], key, key !== 'title'); if (key === 'title') f.input.required = true;
                f.input.disabled = !!d.operation; f.input.oninput = () => { d.fields[key] = f.input.value; }; form.append(f.wrap);
            }
            const state = el(doc, 'label', 'wi-field', 'Disposition'), select = el(doc, 'select', 'ui-control'); select.name = 'disposition';
            for (const [key, label] of Object.entries(DISPOSITIONS)) { const option = el(doc, 'option', '', label === 'Ideas' ? 'Idea' : label); option.value = key; select.append(option); } select.value = d.fields.disposition; select.disabled = !!d.operation; select.onchange = () => { d.fields.disposition = select.value; }; state.append(select); form.append(state);
            const archived = el(doc, 'label', 'wi-archive'), checkbox = el(doc, 'input'); checkbox.type = 'checkbox'; checkbox.checked = d.fields.archived; checkbox.disabled = !!d.operation; checkbox.onchange = () => { d.fields.archived = checkbox.checked; }; archived.append(checkbox, doc.createTextNode(' Archived')); form.append(archived);
            linkEditor(d, form);
            if (d.operation || d.moveOp) form.querySelectorAll('input,textarea,select,button').forEach(n => { n.disabled = true; });
            const saveButton = el(doc, 'button', 'btn btn-primary', 'Save item'); saveButton.type = 'submit'; saveButton.disabled = !!(d.operation || d.moveOp); form.append(saveButton);
            form.onsubmit = event => { event.preventDefault(); void save(b, d); }; host.append(form);
        } else { host.append(multiline('Description', item.description), multiline('Outcome', item.outcome)); }
        const notice = el(doc, 'div', 'wi-save-status'); notice.setAttribute('role', 'status'); notice.append(el(doc, 'p', '', d.notice));
        if (d.operation?.state === 'unknown') notice.append(button('Check current saved text', () => openItem(item.id)), button('Retry same request', () => save(b, d)));
        if (d.conflict) {
            const compare = el(doc, 'section', 'wi-conflict'); compare.append(el(doc, 'h4', '', 'Saved elsewhere · your draft is kept'), multiline('Current title', d.conflict.title), multiline('Current description', d.conflict.description), multiline('Current outcome', d.conflict.outcome), el(doc, 'p', 'wi-meta', `Current disposition: ${d.conflict.disposition}. Saving replaces the complete editable item, including links, with your draft.`),
                button('Save draft against this revision', () => save(b, d, true)), button('Use current saved item', () => { d.base = d.conflict; d.fields = fieldsOf(d.conflict); d.conflict = null; d.editing = false; d.notice = ''; void openItem(item.id); })); notice.append(compare);
        } host.append(notice);
        const linked = el(doc, 'section', 'wi-section'); linked.append(el(doc, 'h4', '', 'Linked work and references'));
        for (const ref of item.references || []) {
            const row = el(doc, 'div', 'wi-linked-row');
            if (ref.kind === 'url' && safeReference(ref.value)) { const a = el(doc, 'a', '', ref.label || ref.value); a.href = safeReference(ref.value); a.target = '_blank'; a.rel = 'noopener noreferrer'; row.append(a); }
            else { row.append(el(doc, 'span', 'wi-text', ref.label || ref.value)); }
            if (ref.kind === 'task') {
                const fact = el(doc, 'span', 'wi-meta', 'Not checked'), readButton = button('Read work state', async () => {
                    const sequence = detailSequence; readButton.disabled = true;
                    try { const data = await taskRead(ref.value, { signal: detailController.signal }); if (currentItem(b, item.id) && sequence === detailSequence) fact.textContent = workFact(data); }
                    catch (error) { if (currentItem(b, item.id) && sequence === detailSequence) fact.textContent = `Work state unavailable: ${error.message}`; }
                    finally { if (readButton.isConnected) readButton.disabled = false; }
                }); const record = el(doc, 'a', '', 'Open original work record'); record.href = `/api/tasks/${encodeURIComponent(ref.value)}`; record.target = '_blank'; record.rel = 'noopener noreferrer'; row.append(fact, readButton, record);
            }
            if (ref.kind === 'row') row.append(button('Open source row', async () => { const sequence = detailSequence, current = () => currentItem(b, item.id) && sequence === detailSequence; try { if (!openReference) throw Error('Source-row navigation is unavailable.'); if (await openReference(ref.value, current) && current()) close({ restoreFocus: false }); } catch (error) { if (current()) note(feedback, error.message, true); } }));
            linked.append(row);
        }
        if (!item.references?.length) linked.append(el(doc, 'p', 'wi-empty', 'No work linked.')); host.append(linked);
        for (const [heading, rows] of [['Prerequisites', item.prerequisites || []], ['Needed by', detailItem?.needed_by || []]]) {
            const section = el(doc, 'section', 'wi-section'); section.append(el(doc, 'h4', '', heading));
            for (const link of rows) {
                const row = el(doc, 'div', 'wi-linked-row');
                if (link.missing) row.append(el(doc, 'span', '', `Missing item · ${link.item_id}`));
                else row.append(button(`${link.title || link.item_id} · ${link.project_id || ''} · ${link.disposition || 'state unavailable'}`, () => openLinked(link)));
                if (link.needs) row.append(el(doc, 'p', 'wi-text', link.needs)); section.append(row);
            }
            if (!rows.length) section.append(el(doc, 'p', 'wi-empty', 'None.')); host.append(section);
        }
        const thread = el(doc, 'section', 'wi-thread'); thread.append(el(doc, 'h4', '', 'Discussion'));
        for (const comment of detailItem?.comments || []) {
            const row = el(doc, 'article', 'wi-comment'); row.dataset.wiComment = comment.id;
            row.append(el(doc, 'p', 'wi-meta', `${authorLabel(comment.created_by || comment.author)} · ${when(comment.created_at)}`));
            if (comment.quote) row.append(el(doc, 'blockquote', 'wi-text', comment.quote.text || ''));
            row.append(el(doc, 'p', 'wi-text', comment.text), button('Quote', () => { d.quote = comment; paintDetail(b, d); q('.wi-comment-form textarea')?.focus(); }), transferButton('Bring comment to chat', text => bring(b, text, feedback), comment, row)); thread.append(row);
        }
        if (!detailItem?.comments?.length) thread.append(el(doc, 'p', 'wi-empty', 'No discussion yet.'));
        const commentForm = el(doc, 'form', 'wi-comment-form');
        if (d.quote) { const quote = el(doc, 'blockquote', 'wi-text', d.quote.text); quote.append(button('Remove quote', () => { d.quote = null; paintDetail(b, d); })); commentForm.append(quote); }
        const cf = field('Add to discussion', d.comment, 'comment', true); cf.input.disabled = !!(d.commentOp || d.moveOp); cf.input.oninput = () => { d.comment = cf.input.value; }; commentForm.append(cf.wrap);
        const post = el(doc, 'button', 'btn btn-primary', 'Post comment'); post.type = 'submit'; post.disabled = !!(d.commentOp || d.moveOp); commentForm.append(post);
        commentForm.onsubmit = event => { event.preventDefault(); void addComment(b, d); }; thread.append(commentForm, el(doc, 'p', 'wi-feedback', d.commentNotice));
        if (d.commentOp?.state === 'unknown') thread.append(button('Retry comment with same request', () => addComment(b, d)));
        host.append(thread, el(doc, 'p', 'wi-meta', `Created ${item.created_by?.surface === 'interface' ? authorLabel(item.created_by) : 'by ' + authorLabel(item.created_by)} · ${when(item.created_at)} · Updated ${when(item.updated_at)} · revision ${item.revision}`));
    }
    async function openLinked(link) {
        if (link.project_id && link.project_id !== board.project.id) {
            const b = stateFor(boards.get(link.project_id)?.project || { id: link.project_id, name: link.project_id }); board = b; syncBoard(); void loadList();
        } await openItem(link.item_id);
    }
    async function openItem(id) {
        if (!live() || !board) return;
        const b = board; b.selected = id; detailController?.abort(); detailController = new AbortController(); const sequence = ++detailSequence;
        root.classList.add('wi-has-detail'); q('.wi-detail').setAttribute('aria-busy', 'true');
        try {
            const data = await request('item', { item_id: id }, detailController);
            if (!currentItem(b, id) || sequence !== detailSequence) return;
            if (data.item?.id !== id || typeof data.item.project_id !== 'string' || !Array.isArray(data.comments) || !Array.isArray(data.needed_by)) throw Error('Work Item response is incomplete.');
            if (data.item.project_id !== b.project.id) {
                q('.wi-detail').replaceChildren(el(doc, 'p', 'wi-text', `This item is now in ${data.item.project_id}. Your drafts and pending requests are kept.`), button('Open in current project', () => followItem(b, data.item)));
                return;
            }
            detailItem = data; paintDetail(b, editorFor(b, data.item)); root.querySelectorAll('[data-wi-item]').forEach(n => n.setAttribute('aria-pressed', String(n.dataset.wiItem === b.selected)));
        } catch (error) {
            if (!currentItem(b, id) || sequence !== detailSequence || detailController.signal.aborted) return;
            q('.wi-detail').replaceChildren(el(doc, 'p', 'wi-error', `Item unavailable: ${error.message}`), button('Retry item', () => openItem(id)));
        } finally { if (currentItem(b, id) && sequence === detailSequence) q('.wi-detail').removeAttribute('aria-busy'); }
    }
    function syncBoard() {
        q('[data-wi-project]').textContent = board.project.name || board.project.id;
        q('.wi-search').elements.q.value = board.query; q('[data-wi-archived]').checked = board.archived;
        q('[data-wi-export]').href = boardRoute('export', { project_id: board.project.id }); q('[data-wi-export]').download = `${board.project.id}-work-items.json`;
        paintCapture(); paintList(); root.classList.toggle('wi-has-detail', !!board.selected);
    }
    const quickCapture = createWorkItemCapture({ doc, freshOperation, mutate,
        checkSaved: (id, controller) => request('list', { project_id: id, limit: '100', offset: '0', archived: 'false' }, controller),
        onCaptured(project, item) {
        const b = boards.get(project.id);
        if (b && board === b && live()) void loadList();
        onCaptured?.(project, item);
    } });
    function open(project, itemId = '', { listOnly = false } = {}) {
        if (dead || !project?.id) return;
        if (!live()) returnFocus = doc.activeElement;
        cancelReads(); board = stateFor(project); detailItem = null;
        if (listOnly) board.selected = '';
        root.hidden = false; syncBoard();
        if (itemId || board.selected) void openItem(itemId || board.selected);
        else q('.wi-detail').replaceChildren(el(doc, 'p', 'wi-empty', 'Choose an item to read or discuss.'));
        void loadList(); q('.wi-add input').focus();
    }
    root.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); close(); } }, { signal: lifetime.signal });
    q('[data-wi-close]').addEventListener('click', () => close(), { signal: lifetime.signal });
    q('[data-wi-back]').addEventListener('click', () => root.classList.remove('wi-has-detail'), { signal: lifetime.signal });
    q('.wi-add').addEventListener('submit', event => { event.preventDefault(); void capture(board); }, { signal: lifetime.signal });
    q('.wi-add input').addEventListener('input', event => { board.capture = event.target.value; }, { signal: lifetime.signal });
    q('.wi-search').addEventListener('submit', event => { event.preventDefault(); board.query = event.target.elements.q.value; board.offset = 0; void loadList(); }, { signal: lifetime.signal });
    root.querySelectorAll('[data-wi-filter]').forEach(node => node.addEventListener('click', () => {
        board.filter = node.dataset.wiFilter; board.offset = 0; void loadList();
    }, { signal: lifetime.signal }));
    q('[data-wi-archived]').addEventListener('change', event => { board.archived = event.target.checked; board.offset = 0; void loadList(); }, { signal: lifetime.signal });
    q('[data-wi-refresh]').addEventListener('click', () => { const detailShown = root.classList.contains('wi-has-detail'); void loadList(); if (detailShown && board.selected) void openItem(board.selected); }, { signal: lifetime.signal });
    q('[data-wi-prev]').addEventListener('click', () => { board.offset = Math.max(0, board.offset - board.limit); void loadList(); }, { signal: lifetime.signal });
    q('[data-wi-next]').addEventListener('click', () => { board.offset += board.limit; void loadList(); }, { signal: lifetime.signal });
    return { root, open, close, openCapture: project => quickCapture.open(project),
        attachRoom(project, header) {
            this.detachRoom(project.id); if (!header) return;
            const node = button('', () => open(project), 'btn btn-default wi-room-entry');
            node.setAttribute('aria-label', 'Work Items');
            node.append(el(doc, 'span', 'wi-room-word', 'Work Items'), el(doc, 'span', 'wi-room-short', 'Items'));
            node.dataset.wiRoom = project.id; header.prepend(node); attachments.set(project.id, node);
        },
        detachRoom(id) { attachments.get(id)?.remove(); attachments.delete(id); },
        destroy() { if (dead) return; dead = true; quickCapture.destroy(); close(); lifetime.abort(); for (const c of mutations) c.abort(); mutations.clear(); for (const node of attachments.values()) node.remove(); attachments.clear(); boards.clear(); drafts.clear(); root.remove(); },
    };
}
