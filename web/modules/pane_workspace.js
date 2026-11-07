/** Main and retained project rooms share space, never chat ownership or sockets. */
import { renderPageHeader } from './page_header.js';
import { createRoomHeader, panicButtonHtml } from './chat_header.js';
import { fmt, fmtInto } from './i18n.js';

export function createPaneWorkspace({ panel, body, ws, active, focus, retire, refreshState, openDashboardTab }) {
    const hosts = new Map(), weights = new Map(), dividers = new Map();
    const content = document.getElementById('content');
    const workspace = document.createElement('div'); workspace.id = 'viper-workspace';
    content.before(workspace);
    workspace.append(content, document.getElementById('project-panel-backdrop'), panel);
    let pair = [], collaboration = false, narrow = false, lastFocused = null;
    const tabs = document.createElement('div');
    tabs.className = 'viper-pane-tabs'; tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Open rooms');
    const notice = document.createElement('span'); notice.className = 'viper-pane-announcement'; notice.setAttribute('aria-live', 'polite');
    workspace.prepend(tabs); workspace.append(notice);
    const mainTab = makeTab('Main Chat', content.id, null); tabs.append(mainTab);
    const weight = id => weights.get(id) || 1;
    const room = id => id === null ? content : hosts.get(id)?.el;
    function makeTab(title, controls, id) {
        const tab = document.createElement('button'); tab.type = 'button'; tab.setAttribute('role', 'tab');
        if (id !== null) tab.setAttribute('data-i18n-authored', '');
        tab.textContent = title; tab.setAttribute('aria-controls', controls);
        tab.addEventListener('click', () => { focus(id); room(id)?.querySelector('.chat-messages')?.focus({ preventScroll: true }); });
        tab.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault(); const ids = [null, ...pair], at = ids.indexOf(id);
            const next = event.key === 'Home' ? null : event.key === 'End' ? ids.at(-1) : ids[(at + (event.key === 'ArrowLeft' ? ids.length - 1 : 1)) % ids.length];
            focus(next); (next === null ? mainTab : hosts.get(next)?.tab)?.focus();
        });
        return tab;
    }
    function divider(left, right) {
        const key = `${left}:${right}`;
        if (dividers.has(key)) return dividers.get(key);
        const el = document.createElement('div'); el.className = 'viper-pane-divider'; el.tabIndex = 0;
        el.setAttribute('role', 'separator'); el.setAttribute('aria-orientation', 'vertical');
        el.setAttribute('aria-label', left === null ? 'Resize Main and project panes' : 'Resize project panes');
        el.setAttribute('aria-valuemin', '10'); el.setAttribute('aria-valuemax', '90');
        let drag = null;
        function resize(fraction) {
            const sum = weight(left) + weight(right);
            const a = room(left)?.getBoundingClientRect(), b = room(right)?.getBoundingClientRect();
            const min = a && b ? Math.min(.49, 440 / (a.width + b.width)) : .1;
            const value = Math.max(min, Math.min(1 - min, fraction));
            weights.set(left, sum * value); weights.set(right, sum * (1 - value)); sync();
        }
        el.addEventListener('pointerdown', e => {
            const a = room(left)?.getBoundingClientRect(), b = room(right)?.getBoundingClientRect();
            if (!a || !b) return;
            drag = { x: a.x, width: b.right - a.x }; el.setPointerCapture(e.pointerId); e.preventDefault();
        });
        el.addEventListener('pointermove', e => { if (drag) resize((e.clientX - drag.x) / drag.width); });
        for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) el.addEventListener(type, () => { drag = null; });
        el.addEventListener('keydown', e => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
            e.preventDefault(); const total = weight(left) + weight(right);
            resize(e.key === 'Home' ? .1 : e.key === 'End' ? .9 : weight(left) / total + (e.key === 'ArrowLeft' ? -.02 : .02));
        });
        dividers.set(key, el); (left === null ? workspace : body).append(el); return el;
    }
    function releaseDividers() {
        for (const el of dividers.values()) el.remove();
        dividers.clear();
    }
    function sync() {
        if (active() && pair.includes(active())) lastFocused = active();
        narrow = collaboration && (document.documentElement.dataset.layout === 'stack' || workspace.clientWidth / (pair.length + 1) < 520);
        workspace.classList.toggle('viper-collaboration', collaboration);
        workspace.classList.toggle('viper-compact', narrow);
        workspace.classList.toggle('viper-main-view', collaboration && narrow && !active());
        panel.classList.toggle('viper-paired', collaboration);
        document.body.classList.toggle('viper-workspace-open', collaboration);
        tabs.hidden = !narrow;
        for (const el of dividers.values()) el.hidden = true;
        const total = pair.reduce((sum, id) => sum + weight(id), 0);
        workspace.style.setProperty('--viper-main-weight', `${weight(null)}fr`);
        workspace.style.setProperty('--viper-project-weight', `${total || 1}fr`);
        const columns = [];
        pair.forEach((id, index) => {
            columns.push(`minmax(440px, ${weight(id)}fr)`);
            if (index < pair.length - 1) {
                columns.push('8px'); const el = divider(id, pair[index + 1]);
                el.hidden = !collaboration || narrow; el.style.gridColumn = String(index * 2 + 2);
                el.setAttribute('aria-valuenow', String(Math.round(weight(id) / (weight(id) + weight(pair[index + 1])) * 100)));
            }
        });
        if (collaboration && pair.length) {
            const el = divider(null, pair[0]); el.hidden = narrow;
            el.setAttribute('aria-valuenow', String(Math.round(weight(null) / (weight(null) + weight(pair[0])) * 100)));
        }
        body.style.setProperty('--viper-columns', columns.join(' '));
        for (const [id, host] of hosts) {
            const shown = pair.includes(id) && (!narrow || id === active());
            const page = host.body.querySelector('.chat-instance-panel');
            const leaving = Boolean(page?.hasAttribute('data-leaving'));
            host.el.hidden = !shown && !leaving; host.el.inert = !shown || leaving;
            host.el.toggleAttribute('data-leaving', leaving);
            if (page) page.hidden = !shown && !leaving;
            host.el.toggleAttribute('data-orientation-narrow', shown && host.el.clientWidth < 520);
            const input = host.body.querySelector('textarea.chat-input');
            if (input) {
                const label = fmt('Message {name}…', { name: host.title.textContent });
                input.dataset.i18nFmt = 'Message {name}…';
                if (input.placeholder !== label) input.placeholder = label;
                if (input.getAttribute('aria-label') !== label) input.setAttribute('aria-label', label);
            }
            host.el.classList.toggle('viper-pane-focused', id === active());
            host.tab.hidden = !pair.includes(id); host.tab.setAttribute('aria-selected', String(id === active())); host.tab.tabIndex = id === active() ? 0 : -1;
        }
        mainTab.setAttribute('aria-selected', String(!active())); mainTab.tabIndex = active() ? -1 : 0;
        content.classList.toggle('viper-main-focused', collaboration && !active());
        pair.forEach((id, i) => {
            const host = hosts.get(id); if (!host) return;
            host.el.style.setProperty('--viper-pane-column', String(i * 2 + 1));
            if (tabs.children[i + 1] !== host.tab) tabs.insertBefore(host.tab, tabs.children[i + 1] || null);
        });
    }
    function closeOne(id = active()) {
        if (!id) return;
        const closingActive = id === active();
        const previousFocus = document.activeElement;
        pair = pair.filter(pid => pid !== id);
        weights.delete(id); releaseDividers();
        if (lastFocused === id) lastFocused = null;
        if (!pair.length) collaboration = false;
        const survivor = closingActive ? pair[0] || null : active();
        retire(id);
        focus(survivor);
        sync();
        if (closingActive) room(survivor)?.querySelector('textarea#chat-input, textarea.chat-input')?.focus({ preventScroll: true });
        else if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus({ preventScroll: true });
    }
    function ensure(project) {
        const id = project.id;
        let host = hosts.get(id);
        if (host) {
            const icon = host.title.querySelector('.viper-room-icon');
            host.title.textContent = project.name || id; if (icon) host.title.prepend(icon);
            host.tab.textContent = project.name || id; return host.body;
        }
        const el = document.createElement('section');
        el.className = 'project-panel viper-room-pane open'; el.dataset.paneProject = id;
        el.setAttribute('role', 'region'); el.setAttribute('aria-labelledby', `viper-title-${id}`);
        el.innerHTML = renderPageHeader({ title: project.name || id, titleId: `viper-title-${id}`, named: false,
            showMobileNav: false, variant: 'project', className: 'project-panel-bar project-inspector-header',
            trailingHtml: panicButtonHtml() + '<button class="shell-icon-control project-panel-close" data-pane-close type="button" aria-label="Close project view" title="Close project view">×</button>' });
        el.querySelector('h2').setAttribute('data-i18n-authored', '');
        const mount = document.createElement('div'); mount.className = 'project-panel-body'; el.append(mount); body.append(el);
        el.id = `viper-pane-${id}`; const tab = makeTab(project.name || id, el.id, id); tabs.append(tab);
        const header = createRoomHeader({ bar: el.querySelector('.project-panel-bar'), ws, refreshState, openDashboardTab });
        const activate = () => { if (pair.includes(id) && active() !== id) focus(id); };
        el.addEventListener('focusin', activate); el.addEventListener('pointerdown', activate);
        el.querySelector('[data-pane-close]').addEventListener('click', () => closeOne(id));
        host = { el, body: mount, title: el.querySelector('h2'), tab, header };
        hosts.set(id, host); return mount;
    }
    const focusMain = () => { if (collaboration && active()) focus(null); };
    content.addEventListener('focusin', focusMain); content.addEventListener('pointerdown', focusMain);
    const observer = new ResizeObserver(sync); observer.observe(workspace);
    window.addEventListener('ouro:language-changed', sync);
    return {
        ensure, sync, closeOne, collaborating: () => collaboration,
        // Plain page-lifetime metadata, not retained rooms, DOM or subscriptions.
        snapshot() {
            if (!collaboration) return null;
            return { ids: [...pair], weights: [null, ...pair].map(id => [id, weight(id)]),
                active: active(), lastFocused };
        },
        restore(saved, projects) {
            const available = new Set(projects.filter(p => String(p.lifecycle || 'active') === 'active').map(p => p.id));
            pair = saved.ids.filter(id => available.has(id));
            weights.clear();
            for (const [id, value] of saved.weights) if ((id === null || pair.includes(id)) && Number.isFinite(value) && value > 0) weights.set(id, value);
            collaboration = pair.length > 0;
            lastFocused = pair.includes(saved.lastFocused) ? saved.lastFocused : null;
            return pair.includes(saved.active) ? saved.active : null;
        },
        select(id, beside = false) {
            if (beside) collaboration = true;
            if (pair.includes(id)) { focus(id); sync(); return true; }
            if (beside) { pair = [...pair, id]; weights.set(id, 1); fmtInto(notice, 'Main Chat and {n} projects open.', { n: pair.length }); }
            else if (collaboration) pair = pair.map(pid => pid === (active() || lastFocused || pair[0]) ? id : pid);
            else pair = [id];
            return false;
        },
        keeps: id => pair.includes(id),
        visible: id => pair.includes(id) && (!narrow || id === active()),
        mainVisible: () => !collaboration || !narrow || !active(),
        focusMain() { if (!collaboration) return false; focus(null); return true; },
        update(data) {
            for (const [id, host] of hosts) {
                const project = data?.projects?.find(row => row.id === id);
                if (project) ensure(project);
                host.header?.update(data);
            }
            sync();
        },
        remove(id) { const host = hosts.get(id); if (!host) return; if (lastFocused === id) lastFocused = null; host.header?.destroy(); host.el.remove(); host.tab.remove(); hosts.delete(id); },
        clear() { pair = []; collaboration = false; lastFocused = null; weights.clear(); releaseDividers(); sync(); },
        destroy() {
            window.removeEventListener('ouro:language-changed', sync);
            observer.disconnect(); content.removeEventListener('focusin', focusMain); content.removeEventListener('pointerdown', focusMain);
            for (const id of [...hosts.keys()]) this.remove(id);
            tabs.remove(); for (const el of dividers.values()) el.remove(); notice.remove();
        },
    };
}
