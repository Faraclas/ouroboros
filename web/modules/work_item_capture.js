// Sidebar capture shares the Work Items mutation/receipt owner. No navigation or execution.
import { bindDialogFocus } from './ui_interactions.js';

export function createWorkItemCapture({ doc = document, freshOperation, mutate, checkSaved, onCaptured }) {
    const drafts = new Map();
    let view = null, dead = false;
    const current = entry => !dead && view?.entry === entry;
    function close({ discard = false, restoreFocus = true } = {}) {
        if (!view) return;
        const old = view; view = null;
        if (discard && !old.entry.operation) drafts.delete(old.entry.project.id);
        old.checkController?.abort(); old.disposeFocus({ restoreFocus }); old.backdrop.remove();
    }
    function paint(entry, retainFocus = true) {
        if (!current(entry)) return;
        const { form, status, retry, closeButtons } = view;
        const locked = !!entry.operation;
        form.elements.title.value = entry.title; form.elements.description.value = entry.description;
        form.elements.title.disabled = form.elements.description.disabled = locked;
        form.querySelector('[data-wic-save]').disabled = locked;
        form.querySelector('[data-wic-save]').textContent = entry.operation?.state === 'sending' ? 'Saving…' : 'Add item';
        status.textContent = entry.notice; status.classList.toggle('wi-error', entry.failure);
        retry.hidden = view.check.hidden = entry.operation?.state !== 'unknown';
        retry.disabled = entry.operation?.state === 'sending';
        closeButtons.forEach(n => { n.textContent = locked ? 'Close' : n.dataset.wicCancel === 'header' ? 'Close' : 'Cancel'; });
        const active = doc.activeElement;
        if (retainFocus && (!view.dialog.contains(active) || active.matches(':disabled') || active.closest('[hidden], [inert]'))) {
            (entry.operation?.state === 'unknown' ? retry : locked ? view.dialog : form.elements.title).focus({ preventScroll: true });
        }
    }
    async function save(entry) {
        if (dead || entry.operation?.state === 'sending') return;
        if (!entry.operation) {
            if (!entry.title.trim()) { entry.notice = 'Give the item a title.'; entry.failure = true; paint(entry); view?.form.elements.title.focus(); return; }
            entry.operation = freshOperation('capture', { project_id: entry.project.id, title: entry.title, description: entry.description });
        }
        const operation = entry.operation;
        entry.notice = 'Saving item…'; entry.failure = false;
        try {
            const pending = mutate(operation); paint(entry);
            const data = await pending;
            if (dead) return;
            // A receipt for another target is not confirmation of this capture.
            if (data.item.project_id !== operation.payload.project_id || data.item.title !== operation.payload.title || data.item.description !== operation.payload.description || data.item.disposition !== 'idea') {
                operation.state = 'unknown'; throw Error('The saved item did not match this request.');
            }
            drafts.delete(entry.project.id);
            if (current(entry)) close();
            onCaptured?.(entry.project, data.item);
        } catch (error) {
            if (dead) return;
            entry.failure = true;
            entry.notice = operation.state === 'rejected' ? error.message : 'Save not confirmed. Your draft and exact request are kept for this page; Retry same request cannot create a second item.';
            if (operation.state === 'rejected') entry.operation = null;
            paint(entry);
        }
    }
    function open(project) {
        if (dead || !project?.id) return;
        if (view?.entry.project.id === project.id) { view.form.elements.title.disabled ? view.dialog.focus() : view.form.elements.title.focus(); return; }
        close({ restoreFocus: false });
        let entry = drafts.get(project.id);
        if (!entry) { entry = { project: { id: project.id, name: project.name || project.id }, title: '', description: '', operation: null, notice: '', failure: false }; drafts.set(project.id, entry); }
        // Keep the exact request target even if the sidebar is renamed/reordered.
        const backdrop = doc.createElement('div'); backdrop.className = 'marketplace-modal-backdrop confirm-dialog-backdrop wi-quick-backdrop';
        backdrop.innerHTML = `<section class="marketplace-modal wi-quick-dialog" role="dialog" aria-modal="true" aria-labelledby="wic-title" aria-describedby="wic-target">
            <div class="marketplace-modal-head"><h3 id="wic-title">Add Work Item</h3><button type="button" class="btn btn-default btn-sm" data-wic-cancel="header" aria-label="Close capture">Close</button></div>
            <form><div class="marketplace-modal-body"><p id="wic-target" class="wi-quick-target"></p>
                <label class="ui-field wi-field">Title<input class="ui-control" name="title" required autocomplete="off"></label>
                <label class="ui-field wi-field">Description <span class="wi-meta">Optional</span><textarea class="ui-control" name="description" rows="4"></textarea></label>
                <p class="wi-meta">Saved as Idea. Your conversation stays where it is.</p><p class="wi-quick-status" role="status" aria-live="polite"></p>
                <button type="button" class="btn btn-default" data-wic-check hidden>Check saved items</button><ul class="wi-quick-saved" hidden></ul>
                <button type="button" class="btn btn-default" data-wic-retry hidden>Retry same request</button>
            </div><div class="marketplace-modal-actions"><button type="button" class="btn btn-default" data-wic-cancel="footer">Cancel</button><button type="submit" class="btn btn-primary" data-wic-save>Add item</button></div></form>
        </section>`;
        const dialog = backdrop.querySelector('[role=dialog]'), form = backdrop.querySelector('form');
        backdrop.querySelector('#wic-target').textContent = `Project: ${entry.project.name}`;
        const cancel = () => close({ discard: true });
        backdrop.addEventListener('click', event => { if (event.target === backdrop || event.target.closest('[data-wic-cancel]')) cancel(); });
        form.addEventListener('submit', event => { event.preventDefault(); void save(entry); });
        form.addEventListener('input', event => { if (!entry.operation && ['title', 'description'].includes(event.target.name)) entry[event.target.name] = event.target.value; });
        form.elements.title.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.isComposing || event.keyCode === 229 || event.repeat)) { event.preventDefault(); } });
        backdrop.querySelector('[data-wic-retry]').onclick = () => { if (entry.operation?.state === 'unknown') void save(entry); };
        backdrop.querySelector('[data-wic-check]').onclick = async () => {
            const checkedView = view, controller = new AbortController();
            checkedView.checkController?.abort(); checkedView.checkController = controller; checkedView.check.disabled = true;
            paint(entry);
            try {
                const data = await checkSaved(entry.project.id, controller);
                if (view !== checkedView || dead) return;
                if (!Array.isArray(data.items)) throw Error('Saved items response is incomplete.');
                const list = backdrop.querySelector('.wi-quick-saved'); list.replaceChildren(); list.hidden = false;
                for (const item of data.items) { const row = doc.createElement('li'); row.textContent = `${item.title} · ${item.id}`; list.append(row); }
                entry.notice = 'Current saved items checked. Matching text does not confirm this request; retry still uses its original key.';
                paint(entry);
            } catch (error) { if (view === checkedView && !dead && !controller.signal.aborted) { entry.notice = `Saved items unavailable: ${error.message}`; paint(entry); } }
            finally { if (view === checkedView) checkedView.check.disabled = false; }
        };
        doc.body.append(backdrop);
        view = { entry, backdrop, dialog, form, status: backdrop.querySelector('.wi-quick-status'), retry: backdrop.querySelector('[data-wic-retry]'), check: backdrop.querySelector('[data-wic-check]'), closeButtons: [...backdrop.querySelectorAll('[data-wic-cancel]')], disposeFocus: () => {} };
        paint(entry, false);
        view.disposeFocus = bindDialogFocus(dialog, { initialFocus: entry.operation ? backdrop.querySelector('[data-wic-cancel]') : form.elements.title, onEscape: cancel, preventScroll: true });
    }
    return { open, destroy() { if (dead) return; dead = true; close({ restoreFocus: false }); drafts.clear(); } };
}
