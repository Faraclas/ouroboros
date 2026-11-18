/** Explicit removal preview. Server owns the Close fence and subsequent cleanup. */
import { fetchJson } from './api_client.js';
import { bindDialogFocus } from './ui_interactions.js';

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
        const targets = el('div', 'pm-cleanup-targets'); body.append(summary, targets);
        const actions = el('div', 'marketplace-modal-actions'), submit = el('button', 'btn btn-danger', closing ? 'Close project' : 'Clean selected files'); submit.type = 'button'; submit.disabled = true;
        const back = el('button', 'btn btn-default', 'Cancel'); back.type = 'button'; actions.append(back, submit);
        dialog.append(head, body, actions); backdrop.append(dialog); document.body.append(backdrop);
        let finished = false, reader = null, version = 0, plan = null, disposeFocus;
        const selections = new Map(), confirmations = new Set();
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
            if (mode.value === 'none') { submit.disabled = false; summary.textContent = 'All local files and resources will be retained.'; return; }
            const selected = (plan?.targets || []).filter(t => selections.get(t.id));
            submit.disabled = !plan || selected.some(t => t.warning && !confirmations.has(t.id));
            summary.textContent = `${selected.length} selected for removal · ${(plan?.targets.length || 0) - selected.length} retained. ${selected.some(t => t.warning && !confirmations.has(t.id)) ? 'Extra confirmation is still required below.' : ''}`;
        }
        async function load() {
            reader?.abort(); const current = ++version; plan = null; selections.clear(); confirmations.clear(); targets.replaceChildren(); submit.disabled = true;
            if (mode.value === 'none') { update(); return; }
            reader = new AbortController(); summary.textContent = 'Inspecting recorded targets…';
            try {
                const result = await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/cleanup/plan?mode=${mode.value}`, { cache: 'no-store', signal: AbortSignal.any([reader.signal, AbortSignal.timeout(30000)]) });
                if (finished || current !== version) return;
                if (!Array.isArray(result.targets)) throw Error('Cleanup plan is incomplete.');
                plan = result;
                for (const target of plan.targets) {
                    const row = el('section', 'pm-resource');
                    const choice = el('label', 'pm-cleanup-check'); const check = el('input'); check.type = 'checkbox'; check.checked = !!target.default_selected; check.disabled = !!target.retain_reason;
                    check.setAttribute('aria-label', `Remove ${target.path}`); selections.set(target.id, check.checked && !check.disabled);
                    choice.append(check, el('span', '', `Remove ${target.kind.replaceAll('_', ' ')}`)); row.append(choice, el('p', 'pm-path', target.path));
                    if (target.retain_reason) row.append(el('p', 'pm-note', `Retained · ${retainedExplanation(target.retain_reason)}`));
                    else if (!target.exists) row.append(el('p', 'pm-note', 'Already absent · nothing to remove.'));
                    if (target.warning && !target.retain_reason) {
                        row.append(el('p', 'pm-cleanup-warning', target.warning.message));
                        const facts = target.risk?.facts || {};
                        row.append(el('p', 'pm-note', `Local inspection: ${target.risk?.status || 'unknown'} · tracked changes ${facts.uncommitted ?? 'unknown'} · untracked ${facts.untracked ?? 'unknown'} · ignored ${facts.ignored ?? 'unknown'} · stashes ${facts.stashes ?? 'unknown'} · commits absent from known remote refs ${facts.commits_not_in_remote_tracking ?? 'unknown'}.`));
                        const extra = el('label', 'pm-cleanup-check'), confirm = el('input'); confirm.type = 'checkbox'; confirm.disabled = !check.checked;
                        confirm.setAttribute('aria-label', `Confirm loss of local material at ${target.path}`);
                        extra.append(confirm, el('span', '', 'I understand this local material may be unique and confirm its removal.'));
                        confirm.onchange = () => { if (confirm.checked) confirmations.add(target.id); else confirmations.delete(target.id); update(); };
                        check.onchange = () => { selections.set(target.id, check.checked); confirm.disabled = !check.checked; confirm.checked = false; confirmations.delete(target.id); update(); };
                        row.append(extra);
                    } else check.onchange = () => { selections.set(target.id, check.checked); update(); };
                    if (target.reconstruction) row.append(el('p', 'pm-note', `Before removal, retain reconstruction: ${target.reconstruction.remote_url} · ${target.reconstruction.branch} · ${target.reconstruction.head}`));
                    targets.append(row);
                }
                targets.append(el('p', 'pm-note', plan.coverage));
                for (const gap of plan.gaps || []) targets.append(el('p', 'pm-cleanup-warning', `Unknown: ${gap}`));
                update();
            } catch (error) {
                if (finished || current !== version) return;
                summary.textContent = `Cleanup preview unavailable: ${error.message}. No cleanup is selected. Retry or choose keep everything.`;
                const retry = el('button', 'btn btn-default', 'Retry inspection'); retry.type = 'button'; retry.onclick = load; targets.append(retry);
            }
        }
        mode.onchange = load;
        submit.onclick = () => {
            if (submit.disabled) return;
            if (mode.value === 'none') { finish({}); return; }
            const selected = plan.targets.filter(t => selections.get(t.id));
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
                section.append(el('p', 'pm-path', `${target.path} · reconstruction recorded ${event.at}\n${r.remote_url}\nbranch ${r.branch} · HEAD ${r.head}`));
            }
        } else if (event.phase === 'finished') {
            for (const result of event.results || []) section.append(el('p', 'pm-path', `${result.path} · ${result.status === 'removed' || result.status === 'already_absent' ? 'missing (cleaned ' + event.at + ')' : readable(result.status) + ' ' + event.at}${result.reason ? ` · ${readable(result.reason)}` : ''}`));
        }
    }
    host.append(section);
}
