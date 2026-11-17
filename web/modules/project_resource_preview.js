/** Read-only, on-demand explanations. No cleanup authority, jobs or persisted state. */
import { fetchJson } from './api_client.js';

const el = (tag, cls, text = '') => {
    const node = document.createElement(tag); node.className = cls; node.textContent = text; return node;
};
const note = text => el('p', 'pm-note', text);
const operations = [
    ['Temporary files', 'Existing Storage retirers select their own installation-wide temporary groups. This view selects nothing.'],
    ['Detach association', 'Would remove only the room’s resource link. Files and the starting-folder default remain.'],
    ['Remove external files or a checkout', 'Would remove the explicitly selected files. Other rooms may share them; repository storage and linked checkouts need separate inspection.'],
    ['Retire a branch', 'Would change Git references, not remove the room or necessarily reclaim repository disk space.'],
    ['Remove retained project data', 'Would affect history and saved results, not just close the room. No purge operation is provided here.'],
];

export function createResourcePreview({ project, managedProjectIds, onStorage }) {
    const root = el('details', 'pm-section pm-resource-preview');
    const summary = el('summary', '', 'Cleanup preview — read only'); root.append(summary);
    const body = el('div', 'pm-preview-body'); root.append(body);
    let controller = null, generation = 0, dead = false, loaded = false;
    const inspections = new Set();
    function cancel() { ++generation; controller?.abort(); controller = null;
        for (const pending of inspections) pending.abort(); inspections.clear(); }
    function inspectionControl(section, resourceId) {
        const button = el('button', 'btn btn-default', 'Inspect repository risks'); button.type = 'button';
        const output = el('div', 'pm-preview-inspection'); section.append(button, output);
        button.addEventListener('click', async () => {
            const pending = new AbortController(); inspections.add(pending);
            button.disabled = true; output.replaceChildren(note('Inspecting this recorded location…'));
            try {
                const data = await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/detail?inspect_resource=${encodeURIComponent(resourceId)}`, {
                    cache: 'no-store', signal: AbortSignal.any([pending.signal, AbortSignal.timeout(120000)]),
                });
                if (dead || pending.signal.aborted || !section.isConnected) return;
                const risk = data.resource_inspection;
                if (data.project?.id !== project.id || risk?.resource_id !== resourceId) throw Error('Inspection response is incomplete.');
                output.replaceChildren(note(`${risk.status} · ${risk.arrangement} · observed ${risk.observed_at}`));
                const facts = risk.facts || {}, count = value => value == null ? 'unknown' : String(value);
                output.append(note(`Local changes: ${count(facts.uncommitted)} tracked · ${count(facts.untracked)} untracked · ${count(facts.ignored)} ignored entries · ${count(facts.stashes)} stashes.`));
                output.append(note(`Commits absent from local remote-tracking refs: ${count(facts.commits_not_in_remote_tracking)}. Branches without upstream: ${count(facts.branches_without_upstream)}.`));
                output.append(note(`Local remote-tracking references: ${count(facts.remote_tracking_refs)}.`));
                output.append(note(risk.remote_coverage), note(risk.coverage));
                const locations = el('details', 'pm-section'); locations.append(el('summary', '', 'Git locations and references'));
                locations.append(note(`Branch: ${facts.branch || 'detached / unknown'} · HEAD: ${facts.head || 'unborn / unknown'}`));
                for (const path of [facts.root, facts.git_dir, facts.common_dir, ...(facts.worktrees || [])].filter(Boolean)) locations.append(el('p', 'pm-path', path));
                output.append(locations);
                if (risk.warning) output.append(note(risk.warning));
                for (const gap of risk.gaps || []) output.append(note(`Not inspected: ${gap}`));
            } catch (error) {
                if (!dead && !pending.signal.aborted) output.replaceChildren(note(`Inspection unavailable: ${error.message || error}. Nothing was changed.`));
            } finally { inspections.delete(pending); button.disabled = false; }
        });
    }
    function render(data) {
        if (data?.project?.id !== project.id || !Array.isArray(data?.resource_preview?.targets)
            || !Array.isArray(data.resource_preview.gaps)) throw Error('Preview response is incomplete.');
        const preview = data.resource_preview;
        body.replaceChildren(note('Inspection only. Nothing here determines whether removal is safe or authorized. No cleanup operation is available.'));
        body.append(note(`Observed ${preview.observed_at}. ${preview.coverage}`));
        if (!preview.targets.length) body.append(note('No resource or starting folder is configured. A conversation-only project needs no file cleanup.'));
        for (const target of preview.targets) {
            const section = el('section', 'pm-resource');
            section.append(el('h4', '', target.label || 'Resource'), el('p', 'pm-path', target.path));
            section.append(note(`Resolved spelling: ${target.resolved_path}`), note(`Path identity: ${target.identity_status}.`));
            const protectedPath = target.runtime_overlap || target.references.some(ref => managedProjectIds.includes(ref.project_id));
            if (protectedPath) {
                section.append(note('Excluded from generic cleanup: runtime or continuing habitat location, or an overlapping registered path. Viper manages these resources deliberately.'));
            } else {
                section.append(note('Not classified as removable. Unregistered sharing and continuing habitat use may still exist.'));
            }
            const refs = el('ul', 'pm-preview-references');
            for (const ref of target.references) {
                const relation = { same: 'same location', contains: 'contains registered path', inside: 'inside registered path' }[ref.relation];
                refs.append(el('li', '', `${ref.project_name} (${ref.project_id}) · ${ref.resource_id} · ${relation}${ref.identity_verified ? '' : ' — spelling only; identity unverified'} · ${ref.path}`));
            }
            section.append(el('h4', '', 'Known registered references'), refs);
            section.append(note('Repository risks are read only when you press Inspect below. Backup, size and live/held use remain uninspected.'));
            const obs = data.project.resources.find(item => item.id === target.resource_id)?.observation
                || (target.resource_id === 'starting_folder' ? data.project.working_dir_observation : null);
            if (obs) section.append(note(`Location: ${obs.status}; ${obs.kind}. Git: ${obs.git.status}${obs.git.branch ? ` · branch ${obs.git.branch}` : ''}${obs.git.root ? ` · root ${obs.git.root}` : ''}. Detailed Git arrangement is read on demand below.`));
            inspectionControl(section, target.resource_id);
            body.append(section);
        }
        const footprint = preview.footprint;
        if (footprint) {
            body.append(el('h4', '', 'Recorded project footprint'), note(footprint.coverage));
            for (const record of footprint.entries) {
                const section = el('section', 'pm-resource');
                const origins = { unknown: 'Earlier origin unknown', pre_existing: 'Existed when recorded — not proof it predates the project', project_created: 'Created for this project', attached: 'Attached existing location', cloned: 'Cloned for this project', genesis: 'New project workspace' };
                section.append(el('p', 'pm-path', record.path), note(`${origins[record.origin] || record.origin} · recorded ${record.recorded_at}`));
                section.append(note(`Source: ${record.source}${record.task_id ? ` · task ${record.task_id}` : ''}`));
                if (record.source === 'agent_declaration') section.append(note('Agent-declared origin, not independently proved creation.'));
                if (record.note) section.append(note(record.note));
                if (!record.observation) section.append(note(record.observation_error || 'No first observation recorded.'));
                if (record.observation) section.append(note(`First observation: ${record.observation.status}; branch ${record.observation.git?.branch || 'unknown'}; HEAD ${record.observation.git?.head || 'unknown'}. Not a file snapshot.`));
                if (!preview.targets.some(target => target.path === record.path)) inspectionControl(section, record.id);
                body.append(section);
            }
            for (const path of footprint.legacy_unknown_paths) body.append(note(`Earlier origin unknown: ${path}`));
            if (footprint.generated) {
                body.append(el('h4', '', 'Existing task and output records'), note(footprint.generated.coverage));
                for (const target of footprint.generated.targets) {
                    const section = el('section', 'pm-resource');
                    section.append(el('p', 'pm-path', target.path), note(`${target.origin} · ${target.source} · task ${target.task_id}`));
                    inspectionControl(section, target.id); body.append(section);
                }
                for (const gap of footprint.generated.gaps) body.append(note(gap));
            }
        }
        if (preview.gaps.length) {
            body.append(el('h4', '', 'Coverage gaps'));
            const gaps = el('ul', 'pm-preview-references');
            for (const gap of preview.gaps) gaps.append(el('li', '', gap));
            body.append(gaps);
        }
        body.append(el('h4', '', 'Different operations have different effects'));
        const meanings = el('dl', 'pm-preview-meanings');
        for (const [label, text] of operations) meanings.append(el('dt', '', label), el('dd', '', text));
        body.append(meanings, note('Close retains the room’s data. Reopen, working-location setup and resource associations remain separate. This preview changes none of them.'));
        const refresh = el('button', 'btn btn-default', 'Refresh preview'); refresh.type = 'button'; refresh.addEventListener('click', () => load()); body.append(refresh);
        if (onStorage) {
            const storage = el('button', 'btn btn-default', 'Open installation-wide Storage'); storage.type = 'button';
            storage.addEventListener('click', onStorage); body.append(storage);
        }
        body.append(note('Storage → Temporary space is installation-wide. Its groups and largest entries are not a complete list for this project. Opening Storage does not start a scan or cleanup.'));
    }
    async function load() {
        cancel(); const current = generation; controller = new AbortController(); const signal = controller.signal;
        if (body.contains(document.activeElement)) summary.focus({ preventScroll: true });
        const status = note('Loading registered-path observations…'); status.setAttribute('role', 'status');
        const close = el('button', 'btn btn-default', 'Cancel inspection'); close.type = 'button';
        close.addEventListener('click', () => { root.open = false; summary.focus(); }); body.replaceChildren(status, close);
        try {
            const data = await fetchJson(`/api/projects/${encodeURIComponent(project.id)}/detail?resource_preview=1`, {
                cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
            });
            if (dead || !root.open || current !== generation) return;
            const ownedFocus = body.contains(document.activeElement);
            render(data); loaded = true;
            // Refresh replaces its own button, not focus elsewhere in the manager.
            if (ownedFocus) summary.focus({ preventScroll: true });
        } catch (error) {
            if (dead || !root.open || current !== generation || signal.aborted) return;
            const text = note(`Preview unavailable: ${error.message || error}. No cleanup was requested.`); text.setAttribute('role', 'alert');
            const retry = el('button', 'btn btn-default', 'Retry preview'); retry.type = 'button'; retry.addEventListener('click', () => load());
            body.replaceChildren(text, retry);
        }
    }
    function toggle() {
        if (!root.open) { cancel(); loaded = false; body.replaceChildren(); return; }
        if (managedProjectIds.includes(project.id)) {
            body.replaceChildren(note('This habitat project is excluded from generic cleanup previews. Viper manages its continuing resources deliberately.')); return;
        }
        if (!loaded) void load();
    }
    root.addEventListener('toggle', toggle);
    return { root, destroy() { dead = true; cancel(); root.removeEventListener('toggle', toggle); body.replaceChildren(); } };
}
