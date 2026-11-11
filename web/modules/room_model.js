/** One saved room primary, shared by the composer and Project details. No model is inferred from a catalog. */
import { apiClient, fetchJson } from './api_client.js';
import { createModelRolesEditor, MODEL_ACCOUNTS_KEY } from './model_roles.js';
import { parseModelSource } from './route_editor_primitives.js';
import { bindPopoverPosition } from './ui_interactions.js';

let sequence = 0;
const same = (a, b) => !!a && !!b && ['model', 'use_local', 'credential_profile_id'].every(key => a[key] === b[key]);
const node = (doc, tag, cls = '', text = '') => { const el = doc.createElement(tag); el.className = cls; el.textContent = text; return el; };
export const primaryModelLabel = value => value ? `${parseModelSource(value.model).model || value.model}${value.use_local ? ' · Local' : ''}` : 'Model not read';

export function createRoomModel({ projectId = '', doc = document, onSaved = () => {}, onRead = () => {} } = {}) {
    const root = node(doc, 'section', 'room-model-editor');
    const saved = node(doc, 'p', 'ui-field-help'), status = node(doc, 'p', 'ui-field-help'); status.setAttribute('role', 'status');
    const host = node(doc, 'div', 'model-role-editor'); host.id = `room-model-${++sequence}`;
    const actions = node(doc, 'div', 'pm-content-actions');
    const make = (text, run) => { const b = node(doc, 'button', 'btn btn-default', text); b.type = 'button'; b.addEventListener('click', run); return b; };
    let stored = null, base = null, editor = null, dirty = false, dead = false, pending = null, blocked = false;
    let loadSequence = 0, editorSettings = null;
    const abort = new AbortController();
    const read = url => fetchJson(url, { cache: 'no-store', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(30000)]) });
    const draft = () => {
        const values = editor?.collect() || {};
        return { model: values.OUROBOROS_MODEL || '', use_local: !!values.USE_LOCAL_MAIN,
            credential_profile_id: values.USE_LOCAL_MAIN ? '' : values[MODEL_ACCOUNTS_KEY]?.main || '' };
    };
    function paint() {
        saved.textContent = `Saved primary: ${primaryModelLabel(stored)}`;
        host.inert = !!pending;
        apply.disabled = !editor || !stored || !!pending || blocked || !dirty;
        check.disabled = !!pending; apply.textContent = pending ? 'Applying…' : 'Apply';
    }
    function sync(value) {
        if (!value || dead) return;
        stored = { ...value };
        if (!dirty && !pending) {
            base = { ...value };
            if (editor && editorSettings && !same(draft(), stored)) renderEditor();
        }
        paint();
    }
    function renderEditor() {
        editor?.destroy();
        editor = createModelRolesEditor({ hostId: host.id, doc: () => doc, showContext: false,
            onChange() { dirty = true; status.textContent = 'Unsaved choice'; paint(); } });
        editor.load({ ...editorSettings, OUROBOROS_MODEL: stored.model, USE_LOCAL_MAIN: stored.use_local,
            [MODEL_ACCOUNTS_KEY]: { main: stored.credential_profile_id } },
        { providerProfiles: editorSettings?._meta?.setup_contract?.providerProfiles || {}, modelSlots: [
            { slot: 'main', settingKey: 'OUROBOROS_MODEL', inputId: `${host.id}-input`, settingsToggleId: `${host.id}-local`, label: 'Primary model' },
        ] });
        editor.mount();
    }
    async function load({ editorRequired = true } = {}) {
        if (pending) return pending;
        const request = ++loadSequence;
        status.textContent = 'Reading primary model…';
        try {
            const reply = await apiClient.roomPrimaryModel(projectId, abort.signal);
            if (dead || request !== loadSequence) return;
            if (reply.project_id !== projectId || !reply.primary_model) throw Error('Model response belongs to another room or is incomplete.');
            sync(reply.primary_model);
            onRead(reply.primary_model);
            blocked = false;
            if (!editorRequired) { status.textContent = ''; paint(); return; }
            if (dirty) { base = { ...stored }; status.textContent = 'Current model checked. Your draft is kept; Apply compares this version.'; paint(); return; }
            const settings = await read('/api/settings');
            if (dead || request !== loadSequence) return;
            editorSettings = settings;
            if (!dirty) renderEditor();
            status.textContent = dirty ? 'Your draft is kept.' : ''; paint();
            try { const catalog = await read('/api/model-catalog'); if (!dead && request === loadSequence) editor.adoptCatalog(catalog); }
            catch (error) { if (!dead) status.textContent = `Suggestions unavailable: ${error.message}. Your saved selection is kept.`; }
        } catch (error) { if (!dead && request === loadSequence) { status.textContent = `Could not read the room model: ${error.message}`; paint(); } }
    }
    const apply = make('Apply', () => {
        if (apply.disabled) return;
        const problem = editor.validate();
        if (problem) { editor.noteSaveAttempt(); status.textContent = problem; return; }
        const desired = draft();
        ++loadSequence; // no earlier read may replace this write's authoritative readback
        blocked = false;
        pending = (async () => {
            try {
                const reply = await apiClient.ownerPrimaryModel({ project_id: projectId, primary_model: desired, expected_primary_model: base });
                if (dead) return false;
                if (reply.project_id !== projectId || !same(reply.primary_model, desired)) throw Error('Save response did not confirm this room and choice.');
                stored = { ...reply.primary_model }; base = { ...stored }; dirty = false;
                status.textContent = 'Primary saved. A following primary changes between calls; an in-flight call finishes unchanged.';
                onSaved(stored); return true;
            } catch (error) {
                if (!dead) { blocked = true; status.textContent = `Save not confirmed: ${error.message}. Your draft is kept. Check current model before applying again.`; }
                return false;
            } finally { pending = null; if (!dead) paint(); }
        })();
        paint();
    });
    apply.className = 'btn btn-primary'; apply.dataset.roomModelApply = '';
    const check = make('Check current model', () => void load()); check.dataset.roomModelCheck = '';
    const cancel = make('Cancel draft', () => { if (!pending) { dirty = false; base = stored && { ...stored }; if (editorSettings) renderEditor(); status.textContent = ''; paint(); } });
    actions.append(apply, check, cancel);
    root.append(saved, host, node(doc, 'p', 'ui-field-help', projectId
        ? 'This project’s following primary changes between calls; in-flight calls finish unchanged. Helpers, reviewers and Safety keep their own routes. Availability is checked when a call starts.'
        : 'Main’s global primary and its inherited defaults. Project selections stay independent. Changes apply between calls; dispatched calls finish unchanged.'), actions, status);
    paint();
    return { root, load, sync, hasPendingSave: () => !!pending || blocked,
        pendingSave: () => pending || Promise.resolve(!blocked),
        destroy() { dead = true; ++loadSequence; abort.abort(); editor?.destroy(); } };
}

/** A selectable current-model capsule; its larger source/model editor floats above, never lifts the message field. */
export function createComposerModel({ el, row, projectId, onSaved, doc = document }) {
    el.classList.add('chat-capsule', 'chat-model-mode'); el.dataset.known = 'false';
    const head = node(doc, 'button', 'chat-capsule-head ui-hit-area'); head.type = 'button'; head.setAttribute('aria-expanded', 'false');
    const name = node(doc, 'span', 'chat-capsule-name', 'Model'), value = node(doc, 'span', 'chat-capsule-value', 'Not read'); value.dataset.i18nAuthored = '';
    head.append(name, value); el.append(head);
    let opened = false, position = null, last = null, dead = false;
    const panel = node(doc, 'div', 'ui-popup room-model-popup'); panel.hidden = true;
    const model = createRoomModel({ projectId, doc, onRead: sync, onSaved(next) { sync(next); onSaved?.(); } }); panel.append(model.root);
    const capsule = { el, kind: 'model', peers: [], isOpen: () => opened, isSettled: () => true,
        snapClosed: close, measureName() {}, relayout() { position?.reposition(); }, sayHint() {} };
    function close() { opened = false; el.dataset.open = 'false'; panel.hidden = true; position?.destroy(); position = null; head.setAttribute('aria-expanded', 'false'); }
    function sync(next) { if (!next || dead) return; last = next; value.textContent = primaryModelLabel(next); head.setAttribute('aria-label', `Primary model: ${value.textContent}. Change model`); el.dataset.known = 'true'; model.sync(next); }
    async function open() {
        capsule.peers.forEach(peer => peer.snapClosed()); opened = true; el.dataset.open = 'true';
        if (!panel.isConnected) doc.body.append(panel);
        panel.hidden = false; head.setAttribute('aria-expanded', 'true'); position = bindPopoverPosition(panel, { anchor: head });
        await model.load(); if (opened && !dead) { position?.reposition(); panel.querySelector('[data-model-role-model]')?.focus(); }
    }
    const click = () => opened ? close() : void open();
    const outside = event => { if (!el.contains(event.target) && !panel.contains(event.target) && !event.target.closest?.('.ui-model-options')) close(); };
    const key = event => { if (opened && event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); close(); head.focus({ preventScroll: true }); } };
    head.addEventListener('click', click); doc.addEventListener('pointerdown', outside); doc.addEventListener('keydown', key);
    // Initial read is scoped, independent of bounded sidebar presence.
    void model.load({ editorRequired: false });
    return { capsule, syncState(data) { sync(data?.primary_model || last); }, hasPendingSave: model.hasPendingSave, pendingSave: model.pendingSave,
        destroy() { dead = true; close(); model.destroy(); panel.remove(); head.removeEventListener('click', click); doc.removeEventListener('pointerdown', outside); doc.removeEventListener('keydown', key); } };
}
