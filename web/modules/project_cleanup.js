/** Explicit removal preview. Server owns the Close fence and subsequent cleanup. */
import { fetchJson } from './api_client.js';
import { bindDialogFocus } from './ui_interactions.js';
import { formatNativePath } from './ui_primitives.js';

const el = (tag, cls = '', text = '') => { const n = document.createElement(tag); n.className = cls; n.textContent = text; return n; };
const readable = value => String(value || '').replaceAll('_', ' ');
const retainedExplanation = reason => ({
    runtime_or_habitat_overlap: 'Shared Viper installation, runtime, or protected habitat directory.',
    runtime_memory_or_recorded_artifact: 'Runtime data, memory, or recorded task artifact.',
    live_task_or_process_uses_target: 'Work or an owned process is still active. Close stops it first; afterward inspect again and select cleanup.',
    process_cwd_inside: 'A running process has its working directory here. Stop that process, then inspect again.',
    shared_with_another_project: 'Shared with another active project.',
    another_copy_or_copy_ancestor: 'Belongs to another task copy or its parent directory.',
    caller_access_denied: 'Outside permitted workspace access; not inspected.',
    inside_repository_requires_repository_owner: 'Inside a Git repository; manage through the repository itself.',
    git_metadata_requires_repository_owner: 'Git metadata directory; manage through the repository itself.',
    nested_repository_inventory_unknown: 'The bounded nested-repository check could not establish safety. Nothing here will be removed.',
    git_worktree_inventory_unknown: 'Git could not list dependent worktrees. Nothing here will be removed.',
    other_git_worktrees_depend_on_repository: 'Other Git worktrees depend on this repository. Retain it until its Git owner retires them.',
    nested_linked_worktree_requires_repository_owner: 'Contains a linked Git worktree; manage through its owning repository.',
    recorded_output_kept: 'Recorded task output kept in reopen-friendly mode.',
    reconstruction_unavailable: 'No remote/branch reconstruction info available for reopen-friendly removal.',
    new_target_requires_inspection: 'Pending loss inspection.',
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
        label.append(mode); body.append(label, el('p', 'pm-note', 'Complete cleanup preselects eligible temporary material across all pages, not delivered or shared work. Association alone never selects an external resource.'));
        const summary = el('p', 'pm-note'); summary.setAttribute('role', 'status');
        const selectAllRow = el('label', 'pm-cleanup-check pm-cleanup-select-all');
        const selectAll = el('input'); selectAll.type = 'checkbox'; selectAll.disabled = true;
        const selectAllText = el('span', '', 'Select all eligible across whole project');
        selectAllRow.append(selectAll, selectAllText);
        const targets = el('div', 'pm-cleanup-targets');
        const filter = el('form', 'pm-cleanup-mode'), search = el('input', 'ui-control'), find = el('button', 'btn btn-default', 'Find path');
        search.type = 'search'; search.placeholder = 'Filter recorded paths'; search.setAttribute('aria-label', 'Filter recorded paths'); find.type = 'submit'; filter.append(search, find);
        const pages = el('nav', 'pm-pagination'); pages.setAttribute('aria-label', 'Cleanup target pages');
        const previous = el('button', 'btn btn-default', 'Previous'), pageText = el('span'), next = el('button', 'btn btn-default', 'Next');
        previous.type = next.type = 'button'; pages.append(previous, pageText, next);
        body.append(summary, selectAllRow, filter, pages, targets);
        const actions = el('div', 'marketplace-modal-actions'), submit = el('button', 'btn btn-danger', closing ? 'Close project' : 'Clean selected files'); submit.type = 'button'; submit.disabled = true;
        const back = el('button', 'btn btn-default', 'Cancel'); back.type = 'button'; actions.append(back, submit);
        dialog.append(head, body, actions); backdrop.append(dialog); document.body.append(backdrop);
        let finished = false, reader = null, version = 0, plan = null, disposeFocus;
        const selections = new Map(), confirmations = new Map(), excludedIds = new Set(), seen = new Set(), knownWarnings = new Map();
        const rowControls = [];
        let selectAllEligible = true, digest = '', targetsDigest = '', offset = 0, query = '';
        const isTargetSelected = target => {
            if (!target || target.retain_reason || !target.exists) return false;
            if (target.eligible && selectAllEligible) return !excludedIds.has(target.id);
            return selections.has(target.id);
        };
        function finish(value) {
            if (finished) return; finished = true; reader?.abort(); signal?.removeEventListener('abort', abort);
            disposeFocus?.(); backdrop.remove(); resolve(value);
        }
        const abort = () => finish(null);
        cancel.onclick = back.onclick = abort;
        backdrop.onclick = e => { if (e.target === backdrop) abort(); };
        signal?.addEventListener('abort', abort, { once: true });
        disposeFocus = bindDialogFocus(dialog, { initialFocus: mode, onEscape: abort, preventScroll: true });
        function rowStatusText(target, checked, confirmed) {
            if (target.exists === false && !target.protection) return 'Already gone · path no longer exists on disk.';
            if (target.retain_reason) return `Protected · ${retainedExplanation(target.retain_reason)}${target.exists === false ? ' (path not present on disk)' : ''}`;
            if (target.warning) return checked && confirmed ? 'Will remove · loss confirmed.' : 'Needs confirmation · review local work below before removing.';
            return checked ? 'Will remove · ready temporary material.' : 'Eligible — not selected.';
        }
        function update() {
            selectAllRow.hidden = mode.value !== 'complete';
            if (mode.value === 'none') { submit.disabled = !closing; summary.textContent = 'All local files and resources will be retained. No cleanup will run.'; return; }
            const eligibleTotal = plan?.eligible_total ?? 0;
            const protectedTotal = plan?.protected_total ?? plan?.retained_total ?? 0;
            const alreadyGoneTotal = plan?.already_gone_total ?? 0;
            const needsConfTotal = plan?.needs_confirmation_total ?? 0;
            const totalTargets = plan?.total ?? 0;
            selectAll.disabled = !plan || eligibleTotal === 0;
            selectAll.checked = Boolean(plan && eligibleTotal > 0 && selectAllEligible && excludedIds.size === 0);
            selectAll.indeterminate = Boolean(plan && eligibleTotal > 0 && selectAllEligible && excludedIds.size > 0 && excludedIds.size < eligibleTotal);
            selectAllText.textContent = `Select all eligible across whole project (${eligibleTotal} eligible)`;
            const selectedEligibleCount = selectAllEligible ? Math.max(0, eligibleTotal - excludedIds.size) : [...selections.values()].filter(t => t.eligible).length;
            const manualNonEligibleCount = [...selections.values()].filter(t => !t.eligible).length;
            const totalSelected = selectedEligibleCount + manualNonEligibleCount;
            const unconfirmed = [...knownWarnings.values()].filter(t => isTargetSelected(t) && !confirmations.has(t.id)).length;
            const selectedWarnings = selectAllEligible
                ? (plan?.eligible_needs_confirmation_total || 0) - [...excludedIds].filter(id => knownWarnings.has(id)).length
                    + [...knownWarnings.values()].filter(t => !t.eligible && isTargetSelected(t)).length
                : [...knownWarnings.values()].filter(isTargetSelected).length;
            const confirmed = [...confirmations.keys()].filter(id => isTargetSelected(knownWarnings.get(id))).length;
            const willRemoveCount = Math.max(0, totalSelected - selectedWarnings + confirmed);
            submit.disabled = !plan || (!closing && !totalSelected) || unconfirmed > 0;
            const page = plan?.targets || [];
            const pageUnavailable = page.filter(t => t.retain_reason || !t.exists).length;
            const pageSelectable = page.length - pageUnavailable;
            const projectScope = `Project-wide (${totalTargets} total): ${willRemoveCount} will remove · ${Math.max(0, needsConfTotal - confirmed)} needs confirmation · ${protectedTotal} protected · ${alreadyGoneTotal} already gone.`;
            const inspectionScope = plan?.uninspected_total ? ` ${plan.uninspected_total} not yet inspected for loss or Git restrictions; review their pages to classify them.` : '';
            const pageScope = ` This page: ${pageSelectable} selectable, ${pageUnavailable} unavailable.`;
            const unseenNote = selectedWarnings - confirmed > unconfirmed ? ' Unconfirmed targets on other pages will stay unless you review and confirm them separately.' : '';
            const guidance = !totalSelected ? (closing ? ' Nothing selected: closing will keep all local material.' : (page.length && pageUnavailable === page.length ? ' Nothing selectable on this page. Read the reasons below; try another page or path filter.' : (page.length ? ' Select material to clean up.' : ' No recorded paths match this filter.'))) : '';
            summary.textContent = projectScope + inspectionScope + pageScope + unseenNote + guidance + (unconfirmed ? ` Extra confirmation is still required for ${unconfirmed} selected target(s).` : '');
        }
        selectAll.onchange = () => {
            if (selectAll.checked) {
                selectAllEligible = true;
                excludedIds.clear();
            } else {
                selectAllEligible = false;
                excludedIds.clear();
                selections.clear();
                confirmations.clear();
            }
            for (const item of rowControls) {
                const checked = isTargetSelected(item.target);
                if (item.check) item.check.checked = checked;
                if (item.confirm) {
                    item.confirm.disabled = !checked;
                    if (!checked) { item.confirm.checked = false; confirmations.delete(item.target.id); }
                }
                item.status.textContent = rowStatusText(item.target, checked, confirmations.has(item.target.id));
            }
            update();
        };
        async function load(reset = true) {
            const ownedFocus = dialog.contains(document.activeElement);
            reader?.abort(); const current = ++version; plan = null; targets.replaceChildren(); rowControls.length = 0; submit.disabled = true;
            if (reset) { selectAllEligible = mode.value === 'complete'; excludedIds.clear(); selections.clear(); confirmations.clear(); seen.clear(); knownWarnings.clear(); digest = ''; targetsDigest = ''; offset = 0; }
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
                plan = result; digest = result.plan_sha256 || ''; targetsDigest = result.targets_sha256 || '';
                previous.disabled = offset === 0; next.disabled = result.next_offset == null;
                pageText.textContent = `${result.targets.length ? offset + 1 : 0}–${offset + result.targets.length} of ${result.matched_total ?? result.targets.length} matching · ${result.total ?? result.targets.length} total`;
                for (const target of plan.targets) {
                    const row = el('section', 'pm-resource');
                    if (!seen.has(target.id)) {
                        seen.add(target.id);
                        if (mode.value === 'reopen_friendly' && target.default_selected && !target.retain_reason && target.exists) selections.set(target.id, target);
                    }
                    if (target.warning && !target.retain_reason && target.exists) knownWarnings.set(target.id, target);
                    else knownWarnings.delete(target.id);
                    // A later inspection may find different local work without changing the structural plan.
                    if (confirmations.get(target.id) !== target.warning?.token) confirmations.delete(target.id);
                    const statusNode = el('p', 'pm-cleanup-status');
                    const isMissing = target.exists === false && !target.protection;
                    let check = null, confirm = null;
                    if (isMissing) {
                        row.append(el('p', 'pm-cleanup-label', `${readable(target.kind)}`), el('p', 'pm-path', formatNativePath(target.path)));
                        statusNode.textContent = rowStatusText(target, false, false);
                        row.append(statusNode);
                    } else {
                        const checked = isTargetSelected(target);
                        const choice = el('label', 'pm-cleanup-check');
                        check = el('input'); check.type = 'checkbox'; check.checked = checked; check.disabled = !!target.retain_reason;
                        check.setAttribute('aria-label', `Remove ${formatNativePath(target.path)}`);
                        choice.append(check, el('span', '', `Remove ${target.kind.replaceAll('_', ' ')}`));
                        row.append(choice, el('p', 'pm-path', formatNativePath(target.path)));
                        statusNode.textContent = rowStatusText(target, checked, confirmations.has(target.id));
                        row.append(statusNode);
                        if (target.warning && !target.retain_reason) {
                            row.append(el('p', 'pm-cleanup-warning', target.warning.message));
                            const facts = target.risk?.facts || {};
                            row.append(el('p', 'pm-note', `Local inspection: ${target.risk?.status || 'unknown'} · tracked changes ${facts.uncommitted ?? 'unknown'} · untracked ${facts.untracked ?? 'unknown'} · ignored ${facts.ignored ?? 'unknown'} · stashes ${facts.stashes ?? 'unknown'} · commits absent from known remote refs ${facts.commits_not_in_remote_tracking ?? 'unknown'}.`));
                            const extra = el('label', 'pm-cleanup-check');
                            confirm = el('input'); confirm.type = 'checkbox'; confirm.disabled = !checked; confirm.checked = confirmations.has(target.id);
                            confirm.setAttribute('aria-label', `Confirm loss of local material at ${formatNativePath(target.path)}`);
                            extra.append(confirm, el('span', '', 'I understand this local material may be unique and confirm its removal.'));
                            confirm.onchange = () => {
                                if (confirm.checked) confirmations.set(target.id, target.warning.token); else confirmations.delete(target.id);
                                statusNode.textContent = rowStatusText(target, check.checked, confirm.checked);
                                update();
                            };
                            check.onchange = () => {
                                if (target.eligible && selectAllEligible) {
                                    if (check.checked) excludedIds.delete(target.id); else excludedIds.add(target.id);
                                } else {
                                    if (check.checked) selections.set(target.id, target); else selections.delete(target.id);
                                }
                                confirm.disabled = !check.checked; confirm.checked = false; confirmations.delete(target.id);
                                statusNode.textContent = rowStatusText(target, check.checked, false);
                                update();
                            };
                            row.append(extra);
                        } else {
                            check.onchange = () => {
                                if (target.eligible && selectAllEligible) {
                                    if (check.checked) excludedIds.delete(target.id); else excludedIds.add(target.id);
                                } else {
                                    if (check.checked) selections.set(target.id, target); else selections.delete(target.id);
                                }
                                statusNode.textContent = rowStatusText(target, check.checked, false);
                                update();
                            };
                        }
                    }
                    if (target.reconstruction) row.append(el('p', 'pm-note', `Before removal, retain reconstruction: ${target.reconstruction.remote_url} · ${target.reconstruction.branch} · ${target.reconstruction.head}`));
                    rowControls.push({ target, check, confirm, status: statusNode });
                    targets.append(row);
                }
                targets.append(el('p', 'pm-note', plan.coverage));
                for (const gap of plan.gaps || []) targets.append(el('p', 'pm-cleanup-warning', `Unknown: ${gap}`));
                update();
            } catch (error) {
                if (finished || current !== version) return;
                selectAllEligible = mode.value === 'complete'; excludedIds.clear(); selections.clear(); confirmations.clear(); seen.clear(); knownWarnings.clear(); digest = ''; targetsDigest = ''; offset = 0;
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
            const eligibleTotal = plan?.eligible_total ?? 0;
            const selectedEligibleCount = selectAllEligible ? Math.max(0, eligibleTotal - excludedIds.size) : [...selections.values()].filter(t => t.eligible).length;
            const explicitTargets = [...selections.values()].filter(t => !selectAllEligible || !t.eligible);
            if (!selectedEligibleCount && !explicitTargets.length) { if (closing) finish({}); return; }
            const confirmationEntries = new Map();
            for (const t of explicitTargets) if (!t.warning) confirmationEntries.set(t.id, t.selection_token);
            for (const [id, token] of confirmations) if (isTargetSelected(knownWarnings.get(id))) confirmationEntries.set(id, token);
            finish({
                cleanup: {
                    mode: mode.value,
                    selections: explicitTargets.map(t => t.id),
                    confirmations: Object.fromEntries(confirmationEntries),
                    ...(selectAllEligible && eligibleTotal > 0 ? { select_all_eligible: true, excluded_ids: [...excludedIds], targets_sha256: targetsDigest } : {}),
                }
            });
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
