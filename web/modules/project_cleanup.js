/** Explicit removal preview. Server owns the Close fence and subsequent cleanup. */
import { fetchJson } from './api_client.js';
import { bindDialogFocus } from './ui_interactions.js';
import { formatNativePath } from './ui_primitives.js';

const el = (tag, cls = '', text = '') => { const n = document.createElement(tag); n.className = cls; n.textContent = text; return n; };
const readable = value => String(value || '').replaceAll('_', ' ');
const retainedExplanation = reason => ({
    live_task_or_process_uses_target: 'Work or an owned process is still active. Close stops it first; afterward inspect again and select cleanup.',
    process_cwd_inside: 'A running process has its working directory here. Stop that process, then inspect again.',
    nested_repository_inventory_unknown: 'The bounded nested-repository check could not establish safety. Nothing here will be removed.',
    git_worktree_inventory_unknown: 'Git could not list dependent worktrees. Nothing here will be removed.',
    other_git_worktrees_depend_on_repository: 'Other Git worktrees depend on this repository. Retain it until its Git owner retires them.',
}[reason] || readable(reason));

export function openProjectCleanup({ project, closing = true, signal } = {}) {
    if (signal?.aborted) return Promise.resolve(null);
    return new Promise(resolve => {
        const backdrop = el('div', 'marketplace-modal-backdrop confirm-dialog-backdrop');
        const dialog = el('div', 'marketplace-modal confirm-dialog pm-cleanup-dialog');
        dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-labelledby', 'pm-cleanup-title');
        const head = el('div', 'marketplace-modal-head'), heading = el('h3', '', closing ? 'Close project' : 'Clean up project'); heading.id = 'pm-cleanup-title';
        const cancel = el('button', 'btn btn-default', 'Cancel'); cancel.type = 'button'; head.append(heading, cancel);
        const body = el('div', 'marketplace-modal-body');
        body.append(el('p', '', closing ? `Close “${project.name || project.id}”? Unfinished work stops; immediate child rooms move up one level. History and notes stay. Recurring schedules pause until you re-enable them.` : 'Remove only the recorded targets you select. Reopening does not restore deleted files.'));
        const label = el('label', 'pm-cleanup-mode', 'What should happen to local material?');
        const mode = el('select', 'ui-control'); mode.setAttribute('aria-label', 'Cleanup choice');
        for (const [value, text] of [['complete', closing ? 'Close and clean up completely' : 'Clean up completely'], ['reopen_friendly', closing ? 'Close, keep it reopen-friendly' : 'Keep it reopen-friendly'], ['none', closing ? 'Close only (keep everything, as today)' : 'Keep everything']]) {
            const option = el('option', '', text); option.value = value; mode.append(option);
        }
        label.append(mode); body.append(label, el('p', 'pm-note', 'Complete cleanup means selected project temporary material, not undoing delivered or shared work. Association alone never selects an external resource.'));
        const summary = el('p', 'pm-note'); summary.setAttribute('role', 'status');
        const targets = el('div', 'pm-cleanup-targets');
        const filter = el('form', 'pm-cleanup-mode'), search = el('input', 'ui-control'), find = el('button', 'btn btn-default', 'Find path');
        search.type = 'search'; search.placeholder = 'Filter recorded paths'; search.setAttribute('aria-label', 'Filter recorded paths'); find.type = 'submit'; filter.append(search, find);
        const pages = el('nav', 'pm-pagination'); pages.setAttribute('aria-label', 'Cleanup target pages');
        const previous = el('button', 'btn btn-default', 'Previous'), pageText = el('span'), next = el('button', 'btn btn-default', 'Next');
        previous.type = next.type = 'button'; pages.append(previous, pageText, next);
        body.append(summary, filter, pages, targets);
        const actions = el('div', 'marketplace-modal-actions'), submit = el('button', 'btn btn-danger', closing ? 'Close project' : 'Clean selected files'); submit.type = 'button'; submit.disabled = true;
        const back = el('button', 'btn btn-default', 'Cancel'); back.type = 'button'; actions.append(back, submit);
        dialog.append(head, body, actions); backdrop.append(dialog); document.body.append(backdrop);
        let finished = false, reader = null, version = 0, plan = null, disposeFocus;
        const selections = new Map(), confirmations = new Set(), seen = new Set();
        let digest = '', offset = 0, query = '';
        function finish(value) {
            if (finished) return; finished = true; reader?.abort(); signal?.removeEventListener('abort', abort);
            disposeFocus?.(); backdrop.remove(); resolve(value);
        }
        const abort = () => finish(null);
        cancel.onclick = back.onclick = abort;
        backdrop.onclick = e => { if (e.target === backdrop) abort(); };
        signal?.addEventListener('abort', abort, { once: true });
        disposeFocus = bindDialogFocus(dialog, { initialFocus: mode, onEscape: abort, preventScroll: true });
        function update() {
            if (mode.value === 'none') { submit.disabled = !closing; summary.textContent = 'All local files and resources will be retained. No cleanup will run.'; return; }
            const selected = [...selections.values()];
            const unconfirmed = selected.some(t => t.warning && !confirmations.has(t.id));
            submit.disabled = !plan || (!closing && !selected.length) || unconfirmed;
            const page = plan?.targets || [];
            const unavailable = page.filter(t => t.retain_reason).length;
            const scope = `${selected.length} selected for removal across inspected pages. This page: ${page.length - unavailable} selectable, ${unavailable} retained. Unseen targets are not selected; their loss inspection is not complete.`;
            const guidance = !selected.length ? (closing ? ' Nothing selected: closing will keep all local material.' : (page.length && unavailable === page.length ? ' Nothing selectable on this page. Read the retention reasons; try another page or path filter.' : (page.length ? ' Select material to clean up.' : ' No recorded paths match this filter.'))) : '';
            summary.textContent = scope + guidance + (unconfirmed ? ' Extra confirmation is still required.' : '');
        }
        async function load(reset = true) {
            const ownedFocus = dialog.contains(document.activeElement);
            reader?.abort(); const current = ++version; plan = null; targets.replaceChildren(); submit.disabled = true;
            if (reset) { selections.clear(); confirmations.clear(); seen.clear(); digest = ''; offset = 0; }
            previous.disabled = next.disabled = true;
            // Disabling the clicked page button or replacing its focused row can
            // move focus to body, outside the dialog's Escape/Tab owner.
            if (ownedFocus && (!dialog.contains(document.activeElement) || document.activeElement.disabled)) search.focus({ preventScroll: true });
            if (mode.value === 'none') { update(); return; }
            reader = new AbortController(); summary.textContent = 'Inspecting recorded targets…';
            try {
                const params = new URLSearchParams({ mode: mode.value, offset: String(offset), limit: '25', query, plan_sha256: digest });
                const result = await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/cleanup/plan?${params}`, { cache: 'no-store', signal: AbortSignal.any([reader.signal, AbortSignal.timeout(30000)]) });
                if (finished || current !== version) return;
                if (!Array.isArray(result.targets)) throw Error('Cleanup plan is incomplete.');
                plan = result; digest = result.plan_sha256 || '';
                previous.disabled = offset === 0; next.disabled = result.next_offset == null;
                pageText.textContent = `${result.targets.length ? offset + 1 : 0}–${offset + result.targets.length} of ${result.matched_total ?? result.targets.length} matching · ${result.total ?? result.targets.length} total`;
                for (const target of plan.targets) {
                    const row = el('section', 'pm-resource');
                    if (!seen.has(target.id)) {
                        seen.add(target.id);
                        if (target.default_selected && !target.retain_reason) selections.set(target.id, target);
                    }
                    const choice = el('label', 'pm-cleanup-check'); const check = el('input'); check.type = 'checkbox'; check.checked = selections.has(target.id); check.disabled = !!target.retain_reason;
                    check.setAttribute('aria-label', `Remove ${formatNativePath(target.path)}`);
                    choice.append(check, el('span', '', `Remove ${target.kind.replaceAll('_', ' ')}`)); row.append(choice, el('p', 'pm-path', formatNativePath(target.path)));
                    if (target.retain_reason) row.append(el('p', 'pm-note', `Retained · ${retainedExplanation(target.retain_reason)}`));
                    else if (!target.exists) row.append(el('p', 'pm-note', 'Already absent · nothing to remove.'));
                    if (target.warning && !target.retain_reason) {
                        row.append(el('p', 'pm-cleanup-warning', target.warning.message));
                        const facts = target.risk?.facts || {};
                        row.append(el('p', 'pm-note', `Local inspection: ${target.risk?.status || 'unknown'} · tracked changes ${facts.uncommitted ?? 'unknown'} · untracked ${facts.untracked ?? 'unknown'} · ignored ${facts.ignored ?? 'unknown'} · stashes ${facts.stashes ?? 'unknown'} · commits absent from known remote refs ${facts.commits_not_in_remote_tracking ?? 'unknown'}.`));
                        const extra = el('label', 'pm-cleanup-check'), confirm = el('input'); confirm.type = 'checkbox'; confirm.disabled = !check.checked; confirm.checked = confirmations.has(target.id);
                        confirm.setAttribute('aria-label', `Confirm loss of local material at ${formatNativePath(target.path)}`);
                        extra.append(confirm, el('span', '', 'I understand this local material may be unique and confirm its removal.'));
                        confirm.onchange = () => { if (confirm.checked) confirmations.add(target.id); else confirmations.delete(target.id); update(); };
                        check.onchange = () => { if (check.checked) selections.set(target.id, target); else selections.delete(target.id); confirm.disabled = !check.checked; confirm.checked = false; confirmations.delete(target.id); update(); };
                        row.append(extra);
                    } else check.onchange = () => { if (check.checked) selections.set(target.id, target); else selections.delete(target.id); update(); };
                    if (target.reconstruction) row.append(el('p', 'pm-note', `Before removal, retain reconstruction: ${target.reconstruction.remote_url} · ${target.reconstruction.branch} · ${target.reconstruction.head}`));
                    targets.append(row);
                }
                targets.append(el('p', 'pm-note', plan.coverage));
                for (const gap of plan.gaps || []) targets.append(el('p', 'pm-cleanup-warning', `Unknown: ${gap}`));
                update();
            } catch (error) {
                if (finished || current !== version) return;
                selections.clear(); confirmations.clear(); seen.clear(); digest = ''; offset = 0;
                summary.textContent = `Cleanup preview unavailable: ${error.message}. No cleanup is selected. Refresh or choose keep everything.`;
                const retry = el('button', 'btn btn-default', 'Refresh inspection'); retry.type = 'button'; retry.onclick = () => load(true); targets.append(retry);
            }
        }
        mode.onchange = () => { query = ''; search.value = ''; void load(true); };
        filter.onsubmit = e => { e.preventDefault(); query = search.value; offset = 0; void load(false); };
        previous.onclick = () => { offset = Math.max(0, offset - 25); void load(false); };
        next.onclick = () => { if (plan?.next_offset != null) { offset = plan.next_offset; void load(false); } };
        submit.onclick = () => {
            if (submit.disabled) return;
            if (mode.value === 'none') { finish({}); return; }
            const selected = [...selections.values()];
            if (!selected.length) { if (closing) finish({}); return; }
            finish({ cleanup: { mode: mode.value, selections: selected.map(t => t.id), confirmations: Object.fromEntries(selected.map(t => [t.id, t.warning?.token || t.selection_token])) } });
        };
        void load();
    });
}

export function appendCleanupHistory(host, project) {
    const request = project.cleanup_request;
    if (request?.status === 'pending' || request?.status === 'deferred') {
        const status = el('p', 'pm-note', request.status === 'pending' ? 'Cleanup is pending after closure. The server continues even if this page closes. Refresh to see the outcome.' : 'Cleanup is deferred because its owning process may still be running. Wait for that process, then inspect again.');
        const clear = el('button', 'btn btn-default', 'Cancel saved cleanup'); clear.type = 'button';
        clear.onclick = async () => {
            clear.disabled = true;
            try {
                await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/cleanup`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'clear_pending' }) });
                status.textContent = 'Saved cleanup cancelled. This action removed no files. Refresh before reopening or planning another cleanup.';
                clear.remove();
            } catch (error) { status.textContent = `Cannot cancel saved cleanup: ${error.message}`; clear.disabled = false; }
        };
        host.append(status, clear);
    }
    if (request?.status === 'cancelled') host.append(el('p', 'pm-note', 'Saved cleanup cancelled. This action removed no files.'));
    if (request?.result?.error) host.append(el('p', 'pm-cleanup-warning', `Cleanup failed: ${request.result.error}`));
    const history = project.cleanup_history || [];
    if (!history.length) return;
    const section = el('details', 'pm-section'), summary = el('summary', '', 'Cleanup and reconstruction history'); section.append(summary);
    for (const event of history) {
        if (event.phase === 'started') {
            for (const target of event.targets || []) if (target.reconstruction) {
                const r = target.reconstruction;
                section.append(el('p', 'pm-path', `${formatNativePath(target.path)} · reconstruction recorded ${event.at}\n${r.remote_url}\nbranch ${r.branch} · HEAD ${r.head}`));
            }
        } else if (event.phase === 'finished') {
            if (event.unselected_retained_count) section.append(el('p', 'pm-note', `${event.unselected_retained_count} unselected targets retained.`));
            for (const result of event.results || []) section.append(el('p', 'pm-path', `${formatNativePath(result.path)} · ${result.status === 'removed' || result.status === 'already_absent' ? 'missing (cleaned ' + event.at + ')' : readable(result.status) + ' ' + event.at}${result.reason ? ` · ${readable(result.reason)}` : ''}`));
        }
    }
    host.append(section);
}
