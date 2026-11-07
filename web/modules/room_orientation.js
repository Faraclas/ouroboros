/** Personal-client room identity and read-only location; no execution or Git controls. */
import { PAGE_ICONS } from './page_icons.js';
import { fetchJson } from './api_client.js';
import { PROJECT_PURPOSE } from './orientation_config.js';

export function decorateRoomHeader(bar, { main = false } = {}) {
    const title = bar?.querySelector('.app-page-title');
    if (!title || title.querySelector('.viper-room-icon')) return;
    bar.classList.add('viper-oriented-header');
    const icon = title.ownerDocument.createElement('span');
    icon.className = 'viper-room-icon'; icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = PAGE_ICONS[main ? 'chat' : 'projects'];
    title.prepend(icon);
}

/** Abort and sequence belong to this disclosure's lifetime, not a global metadata cache. */
export function createProjectInfo({ projectId, doc = document, read = fetchJson }) {
    const element = doc.createElement('section');
    element.className = 'viper-project-info'; element.setAttribute('aria-label', 'Project info');
    const heading = doc.createElement('div'); heading.className = 'viper-project-info-heading';
    const title = doc.createElement('h3'); title.textContent = 'Project info';
    const retry = doc.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry';
    retry.className = 'viper-project-info-retry'; retry.setAttribute('aria-label', 'Retry project location');
    heading.append(title, retry);
    const kind = doc.createElement('p'); kind.className = 'viper-project-purpose';
    kind.textContent = Object.hasOwn(PROJECT_PURPOSE, projectId) ? PROJECT_PURPOSE[projectId] : 'Project';
    const facts = doc.createElement('dl');
    const scope = doc.createElement('p'); scope.className = 'viper-project-location-scope';
    scope.textContent = 'Configured project location. Task-specific checkouts are not shown.';
    const status = doc.createElement('p'); status.className = 'viper-project-info-status'; status.setAttribute('role', 'status');
    element.append(heading, kind, facts, status, scope);
    let active = false, dead = false, sequence = 0, controller = null;
    function row(label, value, path = false) {
        const dt = doc.createElement('dt'), dd = doc.createElement('dd');
        dt.textContent = label; dd.textContent = value;
        if (path) dd.className = 'viper-location-path';
        facts.append(dt, dd);
    }
    function paint(data) {
        if (data?.schema_version !== 1 || data.project_id !== projectId || !data.folder || !data.git) throw Error('Location response unavailable');
        const { folder, git } = data;
        if (!['none', 'available', 'unavailable'].includes(folder.status) || !['repository', 'not_repository', 'unavailable', 'not_applicable'].includes(git.status)) throw Error('Location response unavailable');
        facts.replaceChildren(); status.textContent = ''; retry.hidden = true;
        if (folder.status === 'none') row('Project location', 'No folder configured');
        else if (folder.status === 'available' && typeof folder.path === 'string' && folder.path) row('Configured folder', folder.path, true);
        else { row('Project location', 'Unavailable'); retry.hidden = false; }
        if (git.status === 'repository') {
            if (typeof git.worktree === 'string' && git.worktree) row('Git worktree', git.worktree, true);
            const branch = typeof git.branch === 'string' ? git.branch : '';
            if (git.head_state === 'detached') row('Git HEAD', `Detached${typeof git.commit === 'string' && git.commit ? ' · ' + git.commit.slice(0, 12) : ''}`);
            else if (git.head_state === 'unborn' && branch) row('Git branch', `${branch} · no commits yet`);
            else if (git.head_state === 'branch' && branch) row('Git branch', branch);
            else { row('Git branch', 'Unavailable'); retry.hidden = false; }
        } else if (git.status === 'not_repository') row('Git', 'Not a Git repository');
        else if (git.status === 'unavailable') { row('Git', 'Unavailable'); retry.hidden = false; }
        // not_applicable is not a negative repository probe and earns no Git claim.
        element.title = typeof data.observed_at === 'string' ? `Project location checked ${data.observed_at}` : '';
    }
    async function refresh() {
        if (!active || dead) return;
        controller?.abort(); controller = new AbortController();
        const current = ++sequence, signal = controller.signal;
        element.title = '';
        facts.replaceChildren(); status.textContent = 'Checking project location…'; retry.hidden = false;
        try {
            const data = await read(`/api/extensions/project-orientation/info?project_id=${encodeURIComponent(projectId)}`, { cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(25000)]) });
            if (dead || !active || current !== sequence) return;
            paint(data);
        } catch (error) {
            if (dead || !active || current !== sequence || signal.aborted) return;
            facts.replaceChildren(); status.textContent = 'Project location unavailable.'; retry.hidden = false;
        }
    }
    retry.addEventListener('click', refresh);
    return {
        element,
        open() { if (dead || active) return; active = true; void refresh(); },
        close() { active = false; ++sequence; controller?.abort(); controller = null; },
        destroy() { dead = true; this.close(); retry.removeEventListener('click', refresh); },
    };
}
