/** Web UI orchestrator: shared state, navigation, page init, WS startup. */

import { createWS } from './modules/ws.js';
import { apiFetch, fetchJson } from './modules/api_client.js';
import { loadVersion } from './modules/utils.js';
import { bindScrollFade } from './modules/scroll_fade.js';
import { initChat, createChatInstance } from './modules/chat.js';
import { createStateSnapshotSequencer } from './modules/chat_activity.js';
import { IDLE_SUMMARY, buildProjectActivityIndex, markWords, reconcileProjectRooms } from './modules/project_activity.js';
import { createStatusMark, markView, paintStatusMark, syncMarkPhases } from './modules/status_mark.js';
import { bindRowMenuGesture } from './modules/row_menu_gesture.js';
import { initFiles } from './modules/files.js';
import { draftPort, getContributions } from './modules/contributions.js';
import { bridgeContributedActions } from './modules/message_actions.js';
import { startHostModules } from './modules/host_modules.js';
import { getHolds, reportFoundHolds } from './modules/holds.js';
import { startVoice } from './modules/voice_controls.js';
import { createNotificationActivation, getNotifier } from './modules/notifications.js';
import { showToast } from './modules/toast.js';
import { apiClient } from './modules/api_client.js';
import { openNewProjectDialog, openProjectRowMenu, repositionProjectRowMenu } from './modules/project_create.js';

import { initLogs } from './modules/logs.js';
import { initEvolution } from './modules/evolution.js';
import { initSettings } from './modules/settings.js';
import { initCosts } from './modules/costs.js';
import { initSkills } from './modules/skills.js';
import { initWidgets } from './modules/widgets.js';
import { initUpdates } from './modules/updates.js';
import { initActivity } from './modules/activity.js';
import { initConsciousnessDashboard } from './modules/consciousness_dashboard.js';
import { initUpdateStatus } from './modules/update_status.js';
import { getInterfaceSize } from './modules/interface_size.js';
import { initDashboard } from './modules/dashboard.js';
import { initProjectsManager } from './modules/projects_manager.js';
import { createWorkItems } from './modules/work_items.js';
import { appendSelection } from './modules/chat_selection.js';
import { initStorage } from './modules/storage.js';
import { SHELL_ICONS } from './modules/page_icons.js';
import { renderNavRows, renderProjectsHeader } from './modules/pages.js';
import { hierarchyRows, hierarchyVisible, parentId, parentCaption } from './modules/project_hierarchy.js';
const collapsedProjectBranches = new Set();
import { descendantUnread, unreadVisible, paintUnread, activeUnreadPath } from './modules/project_unread.js';
import { renderPageHeader } from './modules/page_header.js';
import { createRoomHeader, panicButtonHtml } from './modules/chat_header.js';
import { isStackLayout, measurePinnedBars, onLayoutChange, installPanelSizing, installMobileKeyboard } from './modules/layout.js';
import { createLayerStack } from './modules/layers.js';
import { afterMotion, reducedMotion } from './modules/motion.js';
import { createShellMotion } from './modules/shell_motion.js';
import { createRoomRetirement } from './modules/room_retire.js';
import { createTelegramHost } from './modules/host_telegram.js';
import { DRAWER_DRAG, bindDrawerDrag } from './modules/drawer_drag.js';
import { haptic } from './modules/haptics.js';
import { createPaneWorkspace } from './modules/pane_workspace.js';

import { fmt, markBootRead, pendingBootRead, refreshDictionary, setLanguage, storedLanguage, tr } from './modules/i18n.js';
import { initOnboardingOverlay } from './modules/onboarding_overlay.js';
import { installAltMenuSuppression, installDesktopShellLinkInterceptor } from './modules/ui_helpers.js';

const state = {
    messages: [],
    logs: [],
    dashboard: {},
    activeFilters: { tools: true, llm: true, errors: true, tasks: true, system: true, consciousness: true },
    // Main's number (DESIGN "Unread"): its room revision from each state snapshot against the shared read cursor,
    // null until known; the comparison a Project row makes (owner Q1 = B, 19.10.2026).
    mainVisibleRevision: null,
    mainSeenRevision: null,
    mainSeenLoaded: false,
    activePage: 'chat',
    settingsActiveSubtab: 'providers',
    dashboardActiveSubtab: 'logs',
    beforePageLeave: null,
    // Project-thread isolation SSOT for the live WS fan-out. Initialized to an
    // empty Set (never undefined) so chat.js::isMyThread is deterministic before
    // the first /api/state response; populated by renderProjectsNav.
    projectChatIds: new Set(),
};

// Connect only after modules register listeners.
const ws = createWS();
// Loopback-only debug hook: the Playwright leak test counts live ws listeners
// through this handle (the module-scoped `ws` is unreachable from page.evaluate).
window.__ouroWs = ws;
const beforePageLeaveHandlers = [];
let settingsControls = null;
let dashboardControls = null;
// The Projects collapse is the owner's choice on this device (owner 19.10, 2A): written only by the toggle, read once
// here; a device that never chose, or whose storage is unavailable, starts expanded.
const PROJECTS_COLLAPSED_KEY = 'ouroboros.projects.collapsed';
const navState = {
    activeProjectId: null,
    projectsExpanded: (() => { try { return localStorage.getItem(PROJECTS_COLLAPSED_KEY) !== '1'; } catch { return true; } })(),
    mobileDrawerOpen: false,
};
// The one stack of layers over the room (layers.js): the drawer and the Project panel register here, so Escape,
// the scrim, ×, the system Back and Telegram's Back all close exactly one layer, the top one.
const layers = createLayerStack();
// Loopback-only debug hook, like __ouroWs: browser tests read the stack through it.
window.__ouroLayers = layers;
// Every moving mark on the page rides one wave (status_mark.js), for the page's life.
syncMarkPhases(document);
const primarySidebar = document.getElementById('primary-sidebar');
// The phone shell's motion between places (shell_motion.js): presentation only.
const shellMotion = createShellMotion({ content: document.getElementById('content'),
    isStack: () => isStackLayout() });
// The places come from the one registry (pages.js): Main into the pinned head, the tools into the foot that
// follows the Projects, before the version. Nothing below looks a row up before this.
primarySidebar?.querySelector('.sidebar-head')?.insertAdjacentHTML('beforeend', renderNavRows('head'));
primarySidebar?.querySelector('.viper-sidebar-art')?.insertAdjacentHTML('beforebegin', renderNavRows('foot'));
measurePinnedBars(primarySidebar);  // a short window's row scroll margin under its pinned head and foot
const navProjectsHeader = primarySidebar?.querySelector('.nav-projects-header');
if (navProjectsHeader) navProjectsHeader.innerHTML = renderProjectsHeader();
const navProjectsFacts = navProjectsHeader?.querySelector('.nav-projects-facts');
const navDrawerBackdrop = document.getElementById('nav-drawer-backdrop');
const projectPanelBackdrop = document.getElementById('project-panel-backdrop');
const projectPanel = document.getElementById('project-panel');
const projectPanelBody = document.getElementById('project-panel-body');
const navProjects = document.getElementById('nav-projects');
const navProjectsToggle = document.getElementById('nav-projects-toggle');
const navProjectsCount = document.getElementById('nav-projects-count');
const navProjectsList = document.getElementById('nav-projects-list');
const navMainRow = primarySidebar?.querySelector('.nav-row-main');
const projectInstances = new Map();
const projectPaintRequests = new Map();
// The question reveal the current showing is still making (revealProjectQuestion):
// no read decides during it. A close or another showing retires it.
const projectReveals = new Map();
let knownProjectsJson = '';
let lastProjectRows = [];
const pendingProjectPins = new Set();
let projectRooms = new Map();
let projectActivityIndex = buildProjectActivityIndex();
let activitySocketDisconnected = false;
const navToggleNames = new WeakMap();  // each ☰'s last written name (paintNavToggles)
let shownProjectId = null;  // the Project the last navigation sync showed, so a close is told from no change
// The place the last navigation sync showed: the room a handover leaves (shell_motion.js).
let lastScene = { page: 'chat', project: null };
let panelExits = 0;  // the latest close of the Project panel; a reopen voids an older one's put-away
let releaseMobileKeyboardForDrawer = () => {};
let disposeMobileKeyboard = () => {};
const panes = createPaneWorkspace({ panel: projectPanel, body: projectPanelBody, ws,
    active: () => navState.activeProjectId,
    focus: (id) => { navState.activeProjectId = id; syncNavigationState(); if (!id) void acknowledgeMainAfterPaint(); },
    retire: (id) => { projectNavigationGeneration += 1; projectReveals.delete(id); rooms.retire(id); },
    refreshState: refreshProjectsNav, openDashboardTab });

function setMobileDrawerOpen(open, { sync = true } = {}) {
    const nextOpen = Boolean(open);
    if (nextOpen) releaseMobileKeyboardForDrawer();
    navState.mobileDrawerOpen = nextOpen;
    if (sync) syncNavigationState();
}

// The application's one client-side route: a `#<page>` fragment, honoured once
// on load and validated against the injected sections rather than against a
// duplicated page list — the DOM is the single source of truth, so an unknown
// fragment is ignored instead of painting a blank surface. Deliberately NOT
// written back on navigation: the desktop shell and the Telegram mini app have
// no address bar to read it from.
function pageFromHash() {
    const name = String(window.location.hash || '').replace(/^#/, '').trim();
    return name && document.getElementById(`page-${name}`) ? name : '';
}

let pageNavigationGeneration = 0;
async function showPage(name, options = {}) {
    const navigation = ++pageNavigationGeneration;
    const pageName = String(name || '').trim();
    if (!pageName) return false;
    const changingPage = state.activePage !== pageName;
    if (changingPage) {
        for (const handler of beforePageLeaveHandlers) {
            const canLeave = await handler({ from: state.activePage, to: pageName });
            if (canLeave === false || navigation !== pageNavigationGeneration) return false;
        }
        if (state.activePage === 'chat') {
            suspendedPaneLayout = panes.snapshot();
            mainChat?.closeTransient?.();
        }
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById(`page-${pageName}`)?.classList.add('active');
        state.activePage = pageName;
        // Publish exposure only after the final room layout is restored below.
        // Main's number shows only away from Main; showing it offers its read decision.
        updateUnreadBadge();
        // Main read exposure follows restored compact selection, never an intermediate empty layout.
    }
    const restoredLayout = pageName === 'chat' && restorePaneLayout({ mount: options.closeProject !== false });
    if (options.closeProject !== false && !restoredLayout) {
        if (pageName === 'chat' && panes.collaborating()) { panes.focusMain(); void acknowledgeMainAfterPaint(); }
        else closeProjectPanel({ sync: false, preserveLayout: pageName !== 'chat' });
    }
    if (options.closeDrawer !== false) navState.mobileDrawerOpen = false;
    if (options.sync !== false) {
        syncNavigationState();
        if (changingPage) window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: pageName } }));
        if (pageName === 'chat') void acknowledgeMainAfterPaint();
    }
    return true;
}

async function openSettingsTab(tabName) {
    if (!await showPage('settings')) return false;
    if (settingsControls && typeof settingsControls.activateTab === 'function') {
        settingsControls.activateTab(tabName);
    }
    return true;
}

async function openDashboardTab(tabName) {
    if (!await showPage('dashboard')) return false;
    if (dashboardControls && typeof dashboardControls.activateTab === 'function') {
        dashboardControls.activateTab(tabName);
    }
}

// Main's unread standalone messages, the count a Project's revision gives; unknown counts none.
function mainUnreadCount() {
    const { mainVisibleRevision: revision, mainSeenRevision: seen } = state;
    return Number.isSafeInteger(revision) && Number.isSafeInteger(seen) ? Math.max(0, revision - seen) : 0;
}

function updateUnreadBadge() {
    const btn = document.querySelector('[data-nav-page="chat"]');
    let badge = btn?.querySelector('.unread-badge');
    const count = state.activePage === 'chat' ? 0 : mainUnreadCount();
    if (count > 0) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'unread-badge';
            btn.appendChild(badge);
        }
        badge.textContent = count > 99 ? '99+' : String(count);
    } else if (badge) {
        badge.remove();
    }
    paintNavToggles();
}

// Main's revision from a state snapshot (null: unreadable, keep what was known). A cursor the server has never held is
// set to what Main holds now, as the per-window count this replaced started at zero; a newer one offers the read.
function observeMainRevision(revision) {
    if (Number.isSafeInteger(revision)) state.mainVisibleRevision = revision;
    if (!Number.isSafeInteger(state.mainVisibleRevision)) return;
    // A cursor no read has answered yet is unknown, not unset: read it again, set nothing.
    if (!state.mainSeenLoaded) { refreshSharedProjectSeen(); return; }
    if (state.mainSeenRevision === null) void markMainViewed(state.mainVisibleRevision);
    else if (mainUnreadCount() > 0) {
        refreshSharedProjectSeen();
        if (state.activePage === 'chat') void acknowledgeMainAfterPaint();
    }
    updateUnreadBadge();
}

// The shared cursor only moves forward here (a stale answer never revives a read number). Only an answer that
// carries the key knows it: a failed read leaves it unknown, and an unknown cursor is never initialized.
function mergeMainSeenRevision(prefs) {
    if (!prefs || typeof prefs !== 'object' || !('main_seen_revision' in prefs)) return false;
    state.mainSeenLoaded = true;
    const value = prefs.main_seen_revision;
    const advanced = Number.isSafeInteger(value) && value > (state.mainSeenRevision ?? -1);
    if (advanced) state.mainSeenRevision = value;
    updateUnreadBadge();
    return advanced;
}

async function markMainViewed(revision) {
    try {
        const prefs = await fetchJson('/api/ui/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ main_seen_revision: Math.max(0, Number(revision) || 0) }),
        });
        return mergeMainSeenRevision(prefs);
    } catch {
        return false;  // still unread here: the next snapshot or arrival offers the same revision again
    }
}

// Main reads as a Project room does (DESIGN "Unread"): a revision is acknowledged only once a history read covering it
// was painted while Main is shown and its reader is at the newest message.
let mainPaintRequest = null;
async function acknowledgeMainAfterPaint() {
    const revision = state.mainVisibleRevision;
    if (!panes.mainVisible() || document.getElementById('content').inert || state.activePage !== 'chat' || !mainChat?.refreshHistory || !Number.isSafeInteger(revision)
        || !Number.isSafeInteger(state.mainSeenRevision) || revision <= state.mainSeenRevision) return;
    if (mainPaintRequest && mainPaintRequest.revision >= revision) return mainPaintRequest.promise;
    const promise = (async () => {
        let paint = null;
        try { paint = await mainChat.refreshHistory({ revision }); } catch (err) {
            console.error('Main history paint failed:', err);
        }
        if (paint?.read && Number(paint.revision) === revision && state.activePage === 'chat' && panes.mainVisible() && !document.getElementById('content').inert) await markMainViewed(revision);
    })().finally(() => { if (mainPaintRequest?.promise === promise) mainPaintRequest = null; });
    mainPaintRequest = { revision, promise };
    return promise;
}

// The scrim fades with the drawer (style.css): shown at once when the drawer opens, put away when the drawer's motion
// has run, unless it opened again or a finger took it meanwhile (drawer_drag.js then puts the scrim away itself).
function syncDrawerScrim(painted, settled) {
    if (!navDrawerBackdrop) return;
    if (painted || navDrawerBackdrop.hidden) { navDrawerBackdrop.hidden = !painted; return; }
    settled.then(() => {
        if (!navState.mobileDrawerOpen && !navDrawerBackdrop.classList.contains('dragging')) {
            navDrawerBackdrop.hidden = true;
        }
    });
}

// What a place picked in the open drawer hands over inside the room card (shell_motion.js): the leaving and arriving
// rooms (a page, the Project panel, a Project room in it) and what of it takes the breath.
function handoverBetween(from, to) {
    const projectRoom = id => {
        const page = (rooms.retiring.get(id) || projectInstances.get(id))?.page;
        return page?.closest?.('.viper-room-pane') || page;
    };
    const room = (scene, other) => (scene.project
        ? (other.project ? projectRoom(scene.project) : projectPanel)
        : document.getElementById(`page-${scene.page}`));
    const arriving = room(to, from);
    const breathe = arriving === projectPanel ? [...projectPanel.children].filter((el) => !el.classList.contains('room-veil')) : [arriving];
    return { leaving: [room(from, to)].filter(Boolean), arriving, breathe: breathe.filter(Boolean) };
}

function syncNavigationState() {
    const activeProjectId = navState.activeProjectId;
    const workspaceOpen = Boolean(activeProjectId) || panes.collaborating();
    const drawerOpen = Boolean(navState.mobileDrawerOpen);
    // A place chosen in the open drawer changes inside the room card as the drawer closes (owner 20.10, 4A; 21.10).
    const scene = { page: state.activePage, project: activeProjectId || null };
    const handingOver = document.body.classList.contains('nav-drawer-open') && !drawerOpen && isStackLayout()
        && (scene.page !== lastScene.page || scene.project !== lastScene.project);
    if (handingOver) {
        if (activeProjectId && projectPanel) {
            projectPanel.hidden = false;
            projectPanel.classList.add('open');
            void projectPanel.offsetWidth;
        }
        shellMotion.handover(handoverBetween(lastScene, scene));
    }
    lastScene = scene;
    document.body.classList.toggle('nav-drawer-open', drawerOpen);
    primarySidebar?.classList.toggle('open', drawerOpen);
    document.querySelectorAll('[data-mobile-nav-toggle]').forEach((button) => {
        button.setAttribute('aria-expanded', drawerOpen ? 'true' : 'false');
    });
    // The end of the room card's motion: the covered room's inert, the scrim and a room that was left wait for it.
    const settled = shellMotion.settled();
    syncDrawerScrim(drawerOpen, settled);
    // In the stack layout the closed drawer lies under the room: inert and out of the tab order, from the tap.
    const sidebarInert = isStackLayout() && !drawerOpen;
    if (primarySidebar && primarySidebar.inert !== sidebarInert) primarySidebar.inert = sidebarInert;

    document.querySelectorAll('[data-nav-page]').forEach((button) => {
        const isActive = !activeProjectId && button.dataset.navPage === state.activePage;
        button.classList.toggle('active', isActive);
        if (isActive) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    // One chosen row at a time: the open Project's row, never the Projects header (owner Q6).
    navProjectsToggle?.setAttribute('aria-expanded', navState.projectsExpanded ? 'true' : 'false');
    document.querySelectorAll('[data-project-id]').forEach((button) => {
        const isActive = button.dataset.projectId === activeProjectId;
        button.classList.toggle('active', isActive);
        if (isActive) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    let exit = Promise.resolve();
    if (projectPanel) {
        if (workspaceOpen) {
            panelExits += 1;
            projectPanel.hidden = false;
            if (projectPanelBackdrop) projectPanelBackdrop.hidden = false;
            requestAnimationFrame(() => projectPanelBackdrop?.classList.add('open'));
            arrivePanel(activeProjectId, performance.now());
        } else if (!projectPanel.hidden) {
            // The panel leaves with its room in it and is hidden once its own motion has run (motion.js): docked it
            // slides home, on a phone or with reduced motion it fades.
            const closing = ++panelExits;
            projectPanel.classList.remove('open');
            projectPanelBackdrop?.classList.remove('open');
            exit = Promise.all(['transform', 'opacity'].map((property) => afterMotion(projectPanel, { property }).done)).then(() => {
                if (closing !== panelExits || navState.activeProjectId || panes.collaborating()) return;
                projectPanel.hidden = true;
                if (projectPanelBackdrop) projectPanelBackdrop.hidden = true;
            });
        }
        document.body.classList.toggle('project-panel-open', workspaceOpen);
    }
    // A Project room that was left is put away once the motion that shows it leaving has run.
    const leftNow = rooms.exits();
    if (leftNow.length) Promise.all([settled, exit]).then(() => {
        leftNow.forEach(([pid, inst, at]) => rooms.settle(pid, inst, true, at));
        panes.sync(); // Pending owners survive retirement; hide their now-settled wrapper too.
    });
    // One commit for both layers (layers.js), after the panel's unhide so focus can go into it: the drawer, and the
    // Project panel's layer, modal only where it covers the room; on a phone only the covered room's inert (the whole
    // room restyles) waits for the card's motion.
    layers.batch(() => {
        if (drawerOpen) {
            layers.push({ id: 'drawer', el: primarySidebar, modal: true, live: [navDrawerBackdrop].filter(Boolean),
                onClose: () => setMobileDrawerOpen(false) });
        } else layers.remove('drawer');
        if (workspaceOpen) {
            layers.push({ id: 'project', el: panes.collaborating() ? document.getElementById('viper-workspace') : projectPanel, modal: isStackLayout(), live: [projectPanelBackdrop].filter(Boolean),
                onClose: () => closeProjectPanel(), focus: focusProjectRoom });
        } else layers.remove('project');
    }, { inertAfter: isStackLayout() ? settled : null });
    // The layer's inert release is registered first. A delayed read behind the drawer keeps its notice pending.
    Promise.all([settled, exit]).then(() => {
        if (navState.mobileDrawerOpen) return;
        const room = navState.activeProjectId ? projectInstances.get(navState.activeProjectId)
            : state.activePage === 'chat' ? mainChat : null;
        room?.showHistoryNotice?.();
    });
    // Last: the open Project's row is marked. A closed Project hands the focus back to its row (layers.js); where the
    // collapsed list hides that row, the focus would be lost, so the Projects toggle takes it.
    syncProjectsCollapse();
    const at = document.activeElement;
    if (shownProjectId && !activeProjectId && !navProjectsToggle?.closest('[inert]')
        && (!at || at === document.body || projectPanel?.contains(at) || at.closest?.('[hidden]'))) {
        navProjectsToggle?.focus({ preventScroll: true });
    }
    shownProjectId = activeProjectId;
    panes.sync();
    paintNavToggles();  // every room keeps its composer navigation
}

// Docked, the panel comes in from the window's edge with its room in it (owner 20.10, Q2 A): it starts once the room
// has painted, or at a slow read's bound, when it comes in with its loading row (a read seen to take seconds).
const PANEL_ARRIVAL_BOUND_MS = 300;
function arrivePanel(pid, since) {
    requestAnimationFrame(() => {
        if (navState.activeProjectId !== pid || projectPanel.classList.contains('open')) return;
        const waits = !isStackLayout() && !reducedMotion() && !projectInstances.get(pid)?.hasPaintedHistory?.();
        if (waits && performance.now() - since < PANEL_ARRIVAL_BOUND_MS) arrivePanel(pid, since);
        else projectPanel.classList.add('open');
    });
}

// Keys scroll what is being read: an opened Project gives the focus to its room's feed (focusable, out of the tab
// order), so End, PageDown and the arrows move the messages, and a press on their text keeps the focus there. With
// the panel itself focused they moved nothing (#1998). The panel takes it only before its room has mounted.
function focusRoomComposer(instance) {
    const input = instance?.composer?.input;
    const navigation = projectNavigationGeneration, pageNavigation = pageNavigationGeneration;
    const selected = navState.activeProjectId, previous = document.activeElement;
    // Closing the drawer may restore its opener before this deferred composer focus.
    // Only the destination composer's own navigation button is that expected return.
    const drawerOpener = input?.closest('.chat-input-wrap')?.querySelector('[data-mobile-nav-toggle]');
    const apply = () => {
        if (navigation !== projectNavigationGeneration || pageNavigation !== pageNavigationGeneration || state.activePage !== 'chat' || selected !== navState.activeProjectId || navState.mobileDrawerOpen) return;
        if (![previous, document.body, input, drawerOpener].includes(document.activeElement)) return;
        if (input?.isConnected && !input.disabled && !input.readOnly && !input.closest('[inert], [hidden]')) input.focus({ preventScroll: true });
    };
    apply();
    if (document.activeElement !== input) void shellMotion.settled().then(apply);
}

function focusProjectRoom() {
    const feed = projectInstances.get(navState.activeProjectId)?.page?.querySelector('.chat-messages') || projectPanel;
    if (!feed.hasAttribute('tabindex')) feed.tabIndex = -1;
    try { feed.focus({ preventScroll: true }); } catch { /* detached */ }
}

// Crossing the switch: a drawer has no place in the docked layout; the Project layer changes its modality.
onLayoutChange((layout) => {
    if (layout === 'docked') navState.mobileDrawerOpen = false;
    shellMotion.cancel();
    syncNavigationState();
});

document.querySelectorAll('[data-nav-page]').forEach(btn => {
    btn.addEventListener('click', () => {
        const showing = showPage(btn.dataset.navPage);
        const navigation = pageNavigationGeneration;
        void showing.then(shown => {
            if (shown && navigation === pageNavigationGeneration && btn.dataset.navPage === 'chat' && state.activePage === 'chat' && !navState.activeProjectId) focusRoomComposer(mainChat);
        });
    });
});
document.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-mobile-nav-toggle]');
    if (!toggle) return;
    setMobileDrawerOpen(!navState.mobileDrawerOpen);
});
navDrawerBackdrop?.addEventListener('click', () => setMobileDrawerOpen(false));
// On a phone the room is the card the drawer pushes aside (style.css, owner 19.10 1A): Main, and a Project open over
// it, move with the drawer, and a veil of each room's own fades its words (one box per frame: --drawer-p is not
// inherited, so neither a child nor a pseudo-element of the room could follow the finger).
const roomEl = document.getElementById('content');
const roomVeils = new Map([roomEl, projectPanel].filter(Boolean).map((room) => {
    const veil = document.createElement('div');
    veil.className = 'room-veil';
    veil.setAttribute('aria-hidden', 'true');
    room.append(veil);
    return [room, veil];
}));
const movingRooms = () => panes.collaborating() ? [document.getElementById('viper-workspace')] : [roomEl, projectPanel?.classList.contains('open') ? projectPanel : null]
    .filter(Boolean).flatMap((room) => [room, roomVeils.get(room)]);
// The drawer follows a finger (drawer_drag.js; DRAWER_DRAG switches it off); ☰ and × remain its controls.
const disposeDrawerDrag = DRAWER_DRAG ? bindDrawerDrag({
    sidebar: primarySidebar, scrim: navDrawerBackdrop, also: movingRooms,
    isOpen: () => navState.mobileDrawerOpen, setOpen: (open) => setMobileDrawerOpen(open),
    isStack: () => isStackLayout(), isCoarse: () => window.matchMedia('(pointer: coarse)').matches,
    onGrab: () => shellMotion.cancel(), onLayout: (sync) => onLayoutChange(() => sync()), onSnap: () => haptic('snap'),
}) : () => {};
const navDrawerClose = document.getElementById('nav-drawer-close');
if (navDrawerClose) {
    navDrawerClose.innerHTML = SHELL_ICONS.close;
    navDrawerClose.addEventListener('click', () => setMobileDrawerOpen(false));
}

// While a Project panel paints, Main defers first hydration (bounded in chat.js).
let projectPanelOpeningSince = 0;
let mainChat;
let evolution = null;  // Dashboard → Evolution, which takes every sequenced snapshot too (its campaign controls)
const stateSnapshots = createStateSnapshotSequencer((data, requestedAt, generation) => {
    data = data && typeof data === 'object' ? data : {};
    // A complete REST body cannot prove absence while the socket is in a
    // disconnect episode. Keep the previous rooms and render them unknown until
    // a post-reconnect snapshot is sequenced.
    projectRooms = reconcileProjectRooms(projectRooms, activitySocketDisconnected ? {} : data).rooms;
    projectActivityIndex = buildProjectActivityIndex(projectRooms, data.project_chat_ids
        ?? (data.projects || lastProjectRows).map((project) => project?.chat_id));
    renderProjectsNav(data.projects || lastProjectRows,
        data.project_chat_ids ?? (data.projects ? undefined : Array.from(state.projectChatIds)), projectActivityIndex);
    if (data.task_bindings) applyTaskBindings(data.task_bindings);  // null: unchanged, never filled here
    hydrateOpenChatsFromState(data, requestedAt, generation);
    evolution?.hydrateStateSnapshot(data);
    panes.update(data);
    observeMainRevision(data.main_visible_revision);
}, undefined, (requestedAt) => {
    markProjectActivityUnknown();
    hydrateOpenChatsFromState(null, requestedAt);
    evolution?.hydrateStateSnapshot(null);
});

const ctx = {
    ws,
    state,
    showPage,
    onReadingLatest: () => void acknowledgeMainAfterPaint(),
    openSettingsTab,
    openDashboardTab,
    isProjectOpening: () => projectPanelOpeningSince > 0,
    stateSnapshots,
    revealQuotedSource,
    setBeforePageLeave: (handler) => {
        if (typeof handler !== 'function') return () => {};
        beforePageLeaveHandlers.push(handler);
        return () => {
            const idx = beforePageLeaveHandlers.indexOf(handler);
            if (idx >= 0) beforePageLeaveHandlers.splice(idx, 1);
        };
    },
};

// Notifications: ONE notifier and ONE subscription per client. Rooms are
// destroyed as the owner navigates (closing a Project panel destroys its chat
// instance), so the subscription cannot live inside a room — it would go silent
// in exactly the case notifications exist for. app.js owns only the click
// destination, the in-app fallback surface and which chats are the owner's;
// every rule lives in modules/notifications.js.
getNotifier().attach({
    ws,
    ownerVisibleChat: (chatId) => chatId === 1 || state.projectChatIds.has(chatId),
});
getNotifier().configure({
    showToast,
    onActivate: createNotificationActivation({
        resolveProject: async (chatId) => {
            const data = await fetchJson(`/api/projects?chat_id=${encodeURIComponent(chatId)}`);
            return data?.projects?.length === 1 ? data.projects[0] : null;
        },
        openProject: (project, options) => {
            closeNotificationReaders();
            return openProjectPanel(project, options);
        },
        unavailable: () => showToast('This notification’s destination is unavailable.'),
        openMain: () => {
            closeNotificationReaders();
            return showPage('chat');
        },
    }),
});

function closeNotificationReaders() {
    // A resolved notification leads to its message, with no document over it.
    mainChat?.closeTransient?.();
    for (const inst of projectInstances.values()) inst.closeTransient?.();
}

const workItems = createWorkItems({
    onCaptured: project => showToast(`Work Item saved as Idea in ${project.name || project.id}.`),
    async sendToChat(projectId, text, { current, signal }) {
        if (!current()) return;
        // A shell focus restoration is not a new owner intent. A subsequent gesture is.
        const intent = new AbortController();
        let movedOn = false;
        const changed = event => { if (event.isTrusted) movedOn = true; };
        for (const type of ['pointerdown', 'keydown', 'input']) document.addEventListener(type, changed, { capture: true, signal: intent.signal });
        try {
            const project = (await fetchJson(`/api/projects/${encodeURIComponent(projectId)}/detail`, { signal, cache: 'no-store' }))?.project;
            if (!current()) return;
            if (!project || String(project.lifecycle || 'active') !== 'active') throw Error('This room is closed or unavailable. Reopen it explicitly before bringing text to chat.');
            if (movedOn) throw Error('You moved on before the room opened. Nothing was added.');
            const opening = openProjectPanel(project, { openOnly: true, besideSource: panes.collaborating(), composerFocus: false, navigationCurrent: current });
            const navigation = projectNavigationGeneration;
            await opening;
            if (!current()) return;
            const instance = projectInstances.get(projectId);
            if (!instance || navigation !== projectNavigationGeneration || state.activePage !== 'chat' || navState.activeProjectId !== projectId) throw Error('The room changed before text was added. Nothing was added.');
            if (movedOn) throw Error('You moved on while the room opened. Nothing was added.');
            const replyCleared = instance.composer.clearReply();
            appendSelection(instance.composer, text);
            if (replyCleared) showToast('Added to the project draft. The earlier Reply target was removed.');
            return true;
        } finally { intent.abort(); }
    },
    async openReference(address, current) {
        const match = /^row:([0-9]+)@/.exec(address);
        if (!match) throw Error('This source-row address is not supported. The exact reference remains available to copy.');
        if (Number(match[1]) === 1 && state.activePage !== 'chat') {
            if (!await showPage('chat') || !current()) return false;
        }
        const result = await revealQuotedSource(address, Number(match[1]), current);
        if (!current()) return false;
        if (result?.state !== 'found') throw Error('The original source row is unavailable.');
        return true;
    },
});
mainChat = initChat(ctx);
initFiles(ctx);

// Skills' contributions (CLIENT_CONTRACT §11): one store, read now and on every lifecycle change; their message
// actions join the row registry, and their page modules mount unless this start skips them (host_modules.js). Voice
// (voice_controls.js) reads their speech engines: the dictation mic, "+" → Add dictation… and Read aloud.
const contributions = getContributions();
contributions.attach({ ws });
const visibleDraft = draftPort(() => projectInstances.get(navState.activeProjectId) || mainChat);
bridgeContributedActions({ store: contributions, draft: visibleDraft });
startVoice({ hosts: startHostModules({ store: contributions, draft: visibleDraft }) });
// The starts the previous page did not outlive are named in the chat that owns each item (holds.js, POST /api/ui/holds).
reportFoundHolds(getHolds(), (payload) => apiClient.reportUiHolds(payload));

function hydrateOpenChatsFromState(data, snapshotRequestedAt) {
    mainChat?.hydrateStateSnapshot?.(data, snapshotRequestedAt);
    for (const instance of projectInstances.values()) {
        instance.hydrateStateSnapshot?.(data, snapshotRequestedAt);
    }
}

// ---------------------------------------------------------------------------
// Multi-project navigation + right thread panel (v6.32.0). Projects come from
// /api/state; each opens as a chat instance bound to its project chat_id.
// Navigation is one state machine now: page, project, and mobile drawer are
// synchronized together so Utilities and Projects can't remain active at once.
// ---------------------------------------------------------------------------
// Single-live-panel policy (P3, owner 7A): at most ONE project chat instance is
// alive; closing or switching destroys the previous one. The exception is an
// instance holding unsendable client state (staged File attachments / an
// in-flight upload): it is hidden and marked instead, so switching to Settings
// mid-upload never drops attachments. Every reopen lands at the newest message
// (owner decision 2026-10-05): a recreated instance opens there, a kept one moves there.

// A cancelled paint can no longer read, so its request is retired with it: a
// reopened pending-work survivor must start its own read, never join that one.
function cancelProjectPaint(pid, inst) {
    inst?.cancelHistoryPaint?.();
    projectPaintRequests.delete(pid);
}

function destroyProjectInstance(pid) {
    const inst = projectInstances.get(pid);
    if (!inst) return;
    projectDrafts.set(pid, inst.composer.getDraft());
    if (inst.hasPendingWork?.()) {
        // Kept for its staged files; a reader it opened does not stay over the next view.
        inst.closeTransient?.();
        inst.page.hidden = true;
        inst.page.dataset.pendingWork = '1';
        cancelProjectPaint(pid, inst);
        return;
    }
    workItems.detachRoom(pid);
    inst.destroy?.();
    projectInstances.delete(pid);
    projectPaintRequests.delete(pid);
    panes.remove(pid);
}

// A Project room that is left keeps its content while it leaves (room_retire.js); app.js says when it has left.
const rooms = createRoomRetirement({ instances: projectInstances, cancelPaint: cancelProjectPaint, destroy: destroyProjectInstance });

// This page's layout and plain text drafts only. Never browser storage or a hidden chat cache.
let suspendedPaneLayout = null;
const projectDrafts = new Map();
function restorePaneLayout({ mount = true } = {}) {
    if (!suspendedPaneLayout) return false;
    const saved = suspendedPaneLayout;
    suspendedPaneLayout = null;
    navState.activeProjectId = panes.restore(saved, lastProjectRows);
    if (!mount) return true; // explicit sidebar navigation selects its destination before mounting or reading
    const reopened = saved.ids.map(id => lastProjectRows.find(p => p.id === id)).filter(p => p && panes.keeps(p.id));
    for (const project of reopened) mountProjectRoom(project);
    syncNavigationState();
    for (const project of reopened) {
        const inst = projectInstances.get(project.id);
        if (panes.visible(project.id)) {
            void inst.showLatest?.();
            void acknowledgeProjectAfterPaint(project, inst, { forcePaint: !inst.hasPaintedHistory?.() });
        }
    }
    return true;
}

let projectNavigationGeneration = 0;
function closeProjectPanel({ sync = true, reveal = false, preserveLayout = false } = {}) {
    if (!preserveLayout) suspendedPaneLayout = null;
    projectNavigationGeneration += 1;
    projectReveals.clear();
    const activeId = navState.activeProjectId;
    const wasCollaborating = panes.collaborating();
    // Visible rooms leave whole; hidden unsendable owners retain their existing custody.
    for (const pid of [...projectInstances.keys()]) rooms.retire(pid);
    panes.clear();
    navState.activeProjectId = null;
    // Anything left is a hidden pending-work survivor; keep it hidden.
    for (const [pid, inst] of projectInstances) {
        inst.closeTransient?.();
        inst.page.hidden = true;
        cancelProjectPaint(pid, inst);
    }
    if (sync) syncNavigationState();
    // Leaving a Project for Main is a return to Main: its newest message (owner
    // decision 2026-10-05). Another page shows Main later through `ouro:page-shown`.
    if (activeId && state.activePage === 'chat' && !reveal && !wasCollaborating) void mainChat?.showLatest?.();
}

function mountProjectRoom(project) {
    const paneMount = panes.ensure(project);
    let inst = projectInstances.get(project.id);
    if (!inst) {
        inst = createChatInstance({
            ...ctx,
            chatId: Number(project.chat_id) || 1,
            projectId: project.id,
            idPrefix: `pchat-${project.id}`,
            mountEl: paneMount,
            asPanel: true,
            title: project.name || project.id,
            // Retry reads, paints and acknowledges the newest revision.
            onHistoryRetry: () => acknowledgeProjectAfterPaint(freshProjectRow(project), null, { forcePaint: true }),
            // Arriving at the newest messages retries a withheld acknowledgement.
            onReadingLatest: () => acknowledgeProjectAfterPaint(freshProjectRow(project)),
        });
        projectInstances.set(project.id, inst);
        workItems.attachRoom(project, paneMount.parentElement.querySelector('.project-panel-bar .app-page-trailing'));
        if (projectDrafts.has(project.id)) inst.composer.setDraft(projectDrafts.get(project.id));
    }
    delete inst.page.dataset.pendingWork;
    inst.page.hidden = false;
    return inst;
}

async function revealMountedProjectSource(project, inst, sourceReveal, navigation) {
    if (!inst) return { state: 'unknown' };
    const reveal = {};
    projectReveals.set(project.id, reveal);
    cancelProjectPaint(project.id, inst);
    try {
        // The accepted room switch cancelled the quoting room's reading claim.
        // Destination painting now belongs to this navigation and its own reader.
        return await inst.revealSource(sourceReveal.address,
            () => navigation === projectNavigationGeneration, sourceReveal.page);
    } finally {
        if (projectReveals.get(project.id) === reveal) projectReveals.delete(project.id);
    }
}

async function openProjectPanel(project, { closeDrawer = true, openOnly = false, taskId = '', quizId = '', sourceReveal = null, besideSource = null, composerFocus = false, navigationCurrent = () => true } = {}) {
    if (!navigationCurrent() || sourceReveal && !sourceReveal.current()) return null;
    if (!project?.id || String(project.lifecycle || 'active') !== 'active') return;
    const navigation = ++projectNavigationGeneration;
    if (besideSource && panes.keeps(project.id)) panes.select(project.id, true);
    if (panes.keeps(project.id) && navState.activeProjectId !== project.id && !sourceReveal && !taskId && projectInstances.has(project.id)) {
        navState.activeProjectId = project.id; if (closeDrawer) navState.mobileDrawerOpen = false; syncNavigationState(); if (composerFocus) focusRoomComposer(projectInstances.get(project.id)); else focusProjectRoom(); return;
    }
    if (navState.activeProjectId === project.id) {
        if (closeDrawer) { navState.mobileDrawerOpen = false; syncNavigationState(); }
        if (sourceReveal) return revealMountedProjectSource(project, projectInstances.get(project.id), sourceReveal, navigation);
        if (!openOnly && !panes.collaborating()) closeProjectPanel();
        else if (taskId && quizId) await revealProjectQuestion(project, projectInstances.get(project.id), taskId, quizId);
        if (composerFocus && !sourceReveal && !taskId) focusRoomComposer(projectInstances.get(project.id));
        return;
    }
    projectReveals.clear();
    // perf2 P4.2: signal chat.js that a panel open is in flight so Main's
    // deferred first hydration yields the CPU to this build/paint.
    projectPanelOpeningSince = Date.now();
    try {
        // One paint for the whole step (the sync below): a handover leaves the room that was on screen.
        const fromUtilityPage = state.activePage !== 'chat';
        const movedToChat = await showPage('chat', { closeProject: false, closeDrawer: false, sync: false });
        if (movedToChat === false || !navigationCurrent() || navigation !== projectNavigationGeneration || sourceReveal && !sourceReveal.current()) return;
        // The room covers Main: a document Main had open closes rather than sit over it.
        mainChat?.closeTransient?.();
        panes.select(project.id, Boolean(besideSource));
        navState.activeProjectId = project.id;
        panes.ensure(project);
        // One live panel: every OTHER project instance is destroyed (or hidden and
        // marked when it holds pending work) before the target is created/shown; the one
        // on screen, left from the open phone drawer, is retired to fade out inside the card.
        rooms.settle(project.id, undefined, false);
        const handingOver = isStackLayout() && navState.mobileDrawerOpen && closeDrawer;
        for (const pid of [...projectInstances.keys()]) {
            if (pid !== project.id && !panes.keeps(pid)) (handingOver ? rooms.retire : destroyProjectInstance)(pid);
        }
        for (const row of lastProjectRows) if (panes.keeps(row.id)) mountProjectRoom(row);
        const inst = mountProjectRoom(project);
        // A reopened pending-work survivor is live again.
        delete inst.page.dataset.pendingWork;
        for (const [pid, other] of projectInstances) {
            other.page.hidden = !panes.keeps(pid);
            if (!panes.keeps(pid)) {
                other.closeTransient?.();
                cancelProjectPaint(pid, other);
            }
        }
        if (closeDrawer) navState.mobileDrawerOpen = false;
        syncNavigationState();
        if (fromUtilityPage) window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: 'chat' } }));
        if (composerFocus && !sourceReveal && !taskId) focusRoomComposer(inst);
        if (sourceReveal) return revealMountedProjectSource(project, inst, sourceReveal, navigation);
        // Once visible, an unread room lands on its newest message (DESIGN "Unread").
        const unread = Math.max(0, Number(freshProjectRow(project).visible_revision) || 0)
            > Math.max(0, Number(state.projectSeenRevision?.[project.id]) || 0);
        if (!(taskId && quizId)) void inst.showLatest?.({ arrive: unread });
        // ACK the exact painted revision; chat.js owns the receipt and skips redundant reads.
        if (taskId && quizId) await revealProjectQuestion(project, inst, taskId, quizId);
        else {
            // A plain reopen cancels any pending question reveal.
            void inst.revealQuestion?.(taskId, quizId);
            await acknowledgeProjectAfterPaint(project, inst, { forcePaint: !inst.hasPaintedHistory?.() });
        }
    } finally {
        projectPanelOpeningSince = 0;
    }
}

// Resolve and read the exact destination before switching; failed lookups retain room/composer state.
async function revealQuotedSource(address, chatId, isCurrent) {
    const navigation = projectNavigationGeneration;
    const current = () => navigation === projectNavigationGeneration && isCurrent();
    let project = null;
    if (chatId !== 1) {
        const data = await fetchJson(`/api/projects?chat_id=${encodeURIComponent(chatId)}`);
        if (!current()) return null;
        project = data?.projects?.length === 1 ? data.projects[0] : null;
        if (!project || project.lifecycle !== 'active' || Number(project.chat_id) !== chatId) return { state: 'unavailable' };
    }
    const page = await apiClient.chatHistory({ chatId, around: address });
    if (!current()) return null;
    if (page?.anchor?.state !== 'found') return page?.anchor || { state: 'unknown' };
    const matches = address.startsWith('task:') ? chatId === 1 && page.anchor.task_id === address.slice(5)
        : page.anchor.row_address === address && page.anchor.chat_id === chatId;
    if (!matches) return { state: 'unknown' };
    if (project) return openProjectPanel(project, { openOnly: true, sourceReveal: { address, page, current: isCurrent } });
    closeProjectPanel({ reveal: true });
    const showing = projectNavigationGeneration;
    return mainChat.revealSource(address, () => showing === projectNavigationGeneration, page);
}

window.addEventListener('ouro:open-task', async ({ detail }) => {
    if (!detail?.task_id || detail.chat_id !== 1 || !await showPage('chat')) return;
    try {
        const result = await revealQuotedSource(`task:${detail.task_id}`, detail.chat_id, () => state.activePage === 'chat');
        if (result && result.state !== 'found') showToast(tr('consciousness.task_unavailable', 'This task is unavailable.'), 'warn');
    } catch { showToast(tr('consciousness.task_unavailable', 'This task is unavailable.'), 'warn'); }
});

function freshProjectRow(project) {
    return lastProjectRows.find((row) => row.id === project.id) || project;
}

// A question opened from Main is revealed BEFORE the read decision: landing on it
// is not reading the newer messages below it. The reveal is one transaction: the
// addressed question owns the viewport before either history or detail I/O (its
// chat-owned generation yields to later navigation), its paint supersedes any
// request in flight, and no acknowledgement decides until the reveal has ended,
// when this one does (DESIGN "Unread") — unless a close or a later
// showing retired it meanwhile: that navigation owns the room's read instead.
async function revealProjectQuestion(project, inst, taskId, quizId) {
    const reveal = {};
    let current = false;
    projectReveals.set(project.id, reveal);
    try {
        const revealed = inst?.revealQuestion?.(taskId, quizId);
        await acknowledgeProjectAfterPaint(project, inst, { forcePaint: true, paintOnly: true });
        await revealed;
    } finally {
        current = projectReveals.get(project.id) === reveal;
        if (current) projectReveals.delete(project.id);
    }
    if (current) await acknowledgeProjectAfterPaint(freshProjectRow(project), inst);
}

// A Project can receive a new visible revision while its panel remains open.
// Coalesce polling updates per Project, but never acknowledge a newer revision
// until a history read covering it has been painted while the reader is at the
// newest messages (DESIGN "Unread"); `paintOnly` paints without that decision.
async function acknowledgeProjectAfterPaint(project, instance = null, { forcePaint = false, paintOnly = false } = {}) {
    if (!project?.id || !panes.visible(project.id)) return;
    const inst = instance || projectInstances.get(project.id);
    if (!inst || inst.page.hidden) return;
    const revision = Math.max(0, Number(project.visible_revision) || 0);
    const alreadySeen = Math.max(0, Number(state.projectSeenRevision?.[project.id]) || 0);
    if (!forcePaint && revision <= alreadySeen) return;

    // A question paint never joins an acknowledging request (it would inherit its
    // decision); a request may join a question paint. One that overlaps a question
    // reveal paints but decides nothing.
    const current = projectPaintRequests.get(project.id);
    if (current && current.revision >= revision && (current.paintOnly || !paintOnly)) return current.promise;
    inst.cancelHistoryPaint?.();
    const revealing = projectReveals.has(project.id);
    const promise = (async () => {
        let paint = null;
        // A failed read is drawn by the instance itself (error + Retry in its
        // history control) and resolves unpainted. A rejection here is a defect,
        // not a slow network: it is reported, and still never acknowledged.
        try { paint = await inst.refreshHistory?.({ revision }); } catch (err) {
            console.error('Project history paint failed:', err);
        }
        if (
            !paintOnly && !revealing && !projectReveals.has(project.id) && paint?.read
            && Number(paint.revision) === revision
            && panes.visible(project.id)
            && projectInstances.get(project.id) === inst
            && !inst.page.hidden
            // A destroyed instance's page reports hidden===false but is detached;
            // a late paint must never ACK a revision nobody saw (GPT#15).
            && inst.page.isConnected
        ) {
            await markProjectViewed(project.id, revision);
        }
    })().finally(() => {
        if (projectPaintRequests.get(project.id)?.promise === promise) {
            projectPaintRequests.delete(project.id);
        }
    });
    projectPaintRequests.set(project.id, { revision, promise, paintOnly });
    return promise;
}

projectPanelBackdrop?.addEventListener('click', () => closeProjectPanel());
navProjectsToggle?.addEventListener('click', () => {
    navState.projectsExpanded = !navState.projectsExpanded;
    try {
        if (navState.projectsExpanded) localStorage.removeItem(PROJECTS_COLLAPSED_KEY);
        else localStorage.setItem(PROJECTS_COLLAPSED_KEY, '1');
    } catch { /* storage unavailable: the choice holds until the page reloads */ }
    syncNavigationState();
});
// The header's count and mark are the toggle's facts: a press on them toggles the list as well.
navProjectsFacts?.addEventListener('click', () => navProjectsToggle?.click());

// Collapse is all or nothing (owner 19.10, 2A): collapsed, the Projects keep only the pinned ones, and the header's mark
// and unread count speak for the rest (docs/DESIGN.md "Shell"); expanded, every row shows. A row that hides gives the
// keyboard to the Projects toggle first, and an open menu of a hidden row closes (project_create.js).
function syncProjectsCollapse() {
    if (!navProjectsList) return;
    const collapsed = !navState.projectsExpanded;
    const descendants = descendantUnread(lastProjectRows, state.projectSeenRevision);
    const activePath = activeUnreadPath(lastProjectRows, navState.activeProjectId);
    navProjects?.toggleAttribute('data-collapsed', collapsed);
    let shown = 0;
    let pinsShown = false;
    let afterPins = null;
    for (const item of navProjectsList.children) {
        if (!item.classList.contains('nav-project-item')) continue;
        const keep = item.hierarchy ? unreadVisible(item.hierarchy, collapsedProjectBranches, !collapsed, descendants, state.projectSeenRevision, hierarchyVisible(item.hierarchy, collapsedProjectBranches, !collapsed), activePath) : !collapsed;
        if (!keep && item.contains(document.activeElement)) navProjectsToggle?.focus({ preventScroll: true });
        item.hidden = !keep;
        if (keep) shown += 1;
        // Pins lead the list; air before the first shown row after them is the group's one boundary, no caption.
        if (keep && item.hasAttribute('data-pinned')) pinsShown = true;
        else if (keep && pinsShown) afterPins ||= item;
    }
    for (const item of navProjectsList.children) item.toggleAttribute('data-after-pins', item === afterPins);
    navProjectsList.hidden = collapsed && shown === 0;
    // An open row menu follows its anchor once row visibility has settled, or closes with its hidden row.
    repositionProjectRowMenu();
}

function renderProjectsNav(projects, projectChatIds, activityIndex = projectActivityIndex) {
    const all = projects || [];
    // Isolation fan-out SSOT: recognize EVERY registered project chat_id (incl.
    // file-less / no-activity / beyond the sidebar summary cap), matching the
    // backend reserved_project_chat_ids, so chat.js::isMyThread never treats a
    // project frame as a main-thread frame on the live WS path. Prefer the
    // COMPLETE /api/state `project_chat_ids` (uncapped); fall back to the (capped)
    // projects array only if that field is absent. Sidebar visibility is a
    // SEPARATE concern (the filtered `rows` below).
    const completeChatIds = Array.isArray(projectChatIds)
        ? projectChatIds
        : all.map(p => Number(p && p.chat_id) || 0);
    state.projectChatIds = new Set(completeChatIds.map(Number).filter(Boolean));
    // Every active Project is visible, including a newly-created empty room. Unread
    // is a monotonic revision comparison, never a timestamp race.
    const seenRevision = state.projectSeenRevision || {};
    const recency = (p) => String(p.last_active_at || p.updated_at || p.created_at || '');
    const isUnread = (p) => Math.max(0, Number(p.visible_revision) || 0)
        > Math.max(0, Number(seenRevision[p.id]) || 0);
    const rows = all
        .filter(p => p && p.id && ['active', 'deleting'].includes(String(p.lifecycle || 'active')))
        .map(p => ({
            ...p,
            _unread: String(p.lifecycle || 'active') === 'active' && isUnread(p),
        }))
        .sort((a, b) => {
            if ((a.pinned === true) !== (b.pinned === true)) return a.pinned === true ? -1 : 1;
            if (a._unread !== b._unread) return a._unread ? -1 : 1;  // unread to the top
            return recency(b).localeCompare(recency(a));
        });
    for (const row of rows) if (row.lifecycle === 'deleting' && panes.keeps(row.id)) panes.closeOne(row.id);
    if (rows.some(p => p._unread)) refreshSharedProjectSeen();
    const json = JSON.stringify(rows.map(p => [
        p.id, p.name, p.chat_id, p.lifecycle, p.visible_revision, p._unread, p.delete_error, p.pinned === true, p.parent_id,
    ]));
    // Activity is a projection over the existing census and must not rebuild
    // the keyed menu: replacing rows here would steal focus from an open menu,
    // rename action or keyboard navigation. Patch marker attributes in place.
    if (json === knownProjectsJson) {
        patchProjectActivityMarkers(activityIndex);
    } else {
        knownProjectsJson = json;
        lastProjectRows = rows;
        paintProjectsNav();
        syncNavigationState();
    }
    // Every snapshot that still shows the open room unread offers the read decision
    // again, so an acknowledgement whose POST failed is retried by the next poll
    // while its reader stays at the newest message; the receipt still decides.
    for (const row of rows) {
        if (row._unread && row.lifecycle === 'active' && panes.visible(row.id)) acknowledgeProjectAfterPaint(row);
        if (row.lifecycle === 'deleting' && panes.keeps(row.id)) panes.closeOne(row.id);
    }
}

// A transport or state-read failure cannot prove that an observed activity has
// ended. Retain its row for the next complete census, but stop motion and make
// the marker explicitly unknown rather than quietly showing stale liveness.
function markProjectActivityUnknown() {
    projectRooms = reconcileProjectRooms(projectRooms).rooms;
    projectActivityIndex = buildProjectActivityIndex(projectRooms, [...state.projectChatIds]);
    patchProjectActivityMarkers(projectActivityIndex);
}

// A mark's words go into its place's name (its tooltip and accessible name): the hue is never the only way it is told.
function setActivityOwnerName(owner, baseName, words) {
    if (!owner) return;
    const name = words ? `${baseName} · ${words}` : baseName;
    owner.setAttribute('aria-label', name);
    owner.title = name;
}

// Every sidebar mark from the server's rooms (docs/DESIGN.md "Status marks"): a Project row's from its room and its
// unread fact, the Projects header's from all Project rooms (the owner's move, else the strongest work; unread is its
// count), Main's row and the ☰ (paintMainRow, paintNavToggles). Nodes are painted in place, so focus and menus stay.
function patchProjectActivityMarkers(activityIndex = projectActivityIndex) {
    const descendants = descendantUnread(lastProjectRows, state.projectSeenRevision);
    const aggregate = activityIndex.aggregate;
    paintStatusMark(navProjectsFacts?.querySelector('.nav-mark'), markView({ mark: aggregate.mark, owner: aggregate.owner }));
    const unread = navProjectsCount?.title;
    setActivityOwnerName(navProjectsToggle, unread ? `Projects · ${unread}` : 'Projects', aggregate.label);
    const buttons = new Map();
    for (const button of navProjectsList?.querySelectorAll('[data-project-id]') || []) {
        const id = String(button.dataset.projectId || '');
        if (!buttons.has(id)) buttons.set(id, []);
        buttons.get(id).push(button);
    }
    for (const row of lastProjectRows) {
        for (const button of buttons.get(String(row.id)) || []) {
        const summary = activityIndex.byChat.get(String(row.chat_id)) || IDLE_SUMMARY;
        const deleting = String(row.lifecycle || 'active') === 'deleting';
        button.dataset.tone = deleting ? 'unknown' : summary.tone;
        const unreadRow = Boolean(row._unread) && !deleting;
        button?.toggleAttribute('data-unread', unreadRow);
        // A deleting row draws only `Deleting…`; its census fact stays in its name.
        paintStatusMark(button?.querySelector('.nav-mark'),
            deleting ? markView() : markView({ mark: summary.mark, owner: summary.owner }));
        // The pin is said in the row's full name (its tooltip and accessible name), never drawn in the row.
        const name = `${row.name || row.id}${row.pinned === true ? ' · Pinned' : ''}`;
        setActivityOwnerName(button, deleting ? `${name} — Closing…${row.delete_error ? ` ${row.delete_error}` : ''}` : name,
            markWords(summary, false));
        const context = `${parentCaption(row, lastProjectRows)}${button.closest('[data-pin-shortcut]') ? ' · Pinned shortcut' : ''}`;
        const unreadWords = paintUnread(button, row, state.projectSeenRevision, descendants);
        const details = [context, unreadWords].filter(Boolean).join(' · ');
        button.title += ` · ${details}`;
        button.setAttribute('aria-label', `${button.getAttribute('aria-label')} · ${details}`);
        }
    }
    paintMainRow();
    paintNavToggles();
    syncProjectsCollapse();
}

// Main's row carries its room's mark in the ink of the row; its own new messages stay its number badge
// (updateUnreadBadge). Its words are a text for readers inside the row, so the row keeps the name the interface's
// language gives its label (a Project row is named by its owner, so its name is an attribute).
function paintMainRow() {
    const summary = projectRooms.get('1') || IDLE_SUMMARY;
    paintStatusMark(navMainRow?.querySelector('.nav-mark'), markView({ mark: summary.mark, owner: summary.owner }));
    const words = navMainRow?.querySelector('.nav-row-words');
    const text = summary.label ? ` · ${summary.label}` : '';
    if (words && words.textContent !== text) words.textContent = text;
}

// The ☰ tells what waits behind it (owner 19.10, 3A): an amber dot while Main or a Project waits for the owner, else a
// crimson dot while one has new messages, else nothing; its name says the same. Both placements (a page header's, a
// room's composer row) are painted; a composer that mounted later is painted by the navigation sync that shows it.
function paintNavToggles() {
    const owner = (projectRooms.get('1') || IDLE_SUMMARY).owner || projectActivityIndex.aggregate.owner;
    const unread = (state.activePage !== 'chat' && mainUnreadCount() > 0) || lastProjectRows.some((row) => row._unread);
    const view = markView({ owner, unread });
    const name = owner ? 'Open navigation · something waits for you' : unread ? 'Open navigation · new messages' : 'Open navigation';
    for (const toggle of document.querySelectorAll('[data-mobile-nav-toggle]')) {
        paintStatusMark(toggle.querySelector('.status-mark'), view);
        // Written only when it changes, so the interface language's overlay is not asked to translate it again.
        if (navToggleNames.get(toggle) !== name) {
            navToggleNames.set(toggle, name);
            toggle.setAttribute('aria-label', name);
        }
    }
}

// ACK exactly the revision painted. The server max-merges and clamps the cursor,
// so stale tabs cannot move it backwards or acknowledge unseen future output; the
// cursors it answers with, never the requested number, become this client's.
async function markProjectViewed(projectId, revision) {
    if (!projectId) return false;
    const seen = Math.max(0, Number(revision) || 0);
    let prefs;
    try {
        prefs = await fetchJson('/api/ui/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_seen_revision: { [projectId]: seen } }),
        });
    } catch {
        // The room was painted, but the durable monotonic ACK failed. Keep it
        // unread locally so polling or the next open retries the same revision.
        return false;
    }
    mergeProjectSeenRevisions(prefs?.project_seen_revision);
    return true;
}

// Shared read cursors only move forward here, so a late or stale answer can
// clear a dot another client read but never revive one.
function mergeProjectSeenRevisions(cursors) {
    state.projectSeenRevision = state.projectSeenRevision || {};
    let advanced = false;
    for (const [projectId, value] of Object.entries(cursors || {})) {
        const seen = Math.max(0, Number(value) || 0);
        if (seen <= (Number(state.projectSeenRevision[projectId]) || 0)) continue;
        state.projectSeenRevision[projectId] = seen;
        advanced = true;
    }
    if (!advanced || !Array.isArray(lastProjectRows)) return advanced;
    let changed = false;
    for (const row of lastProjectRows) {
        const unread = row._unread && (Number(row.visible_revision) || 0)
            > (Number(state.projectSeenRevision[row.id]) || 0);
        if (row._unread !== unread) { row._unread = unread; changed = true; }
    }
    if (changed) paintProjectsNav();
    else { paintProjectsCount(); patchProjectActivityMarkers(); }  // a cursor moved within an unread room
    return advanced;
}

// Another client may already have read what this one still shows unread. Each
// state snapshot that shows unread re-reads the shared cursors, one read at a time.
let sharedSeenRead = null;
function refreshSharedProjectSeen() {
    sharedSeenRead ||= fetchJson('/api/ui/preferences', { cache: 'no-store' })
        .then((prefs) => { mergeMainSeenRevision(prefs); return mergeProjectSeenRevisions(prefs?.project_seen_revision); },
            () => false)
        .finally(() => { sharedSeenRead = null; });
}

// The Projects number counts what Main's does (owner Q1 = B): unread standalone messages, summed over the rows.
function paintProjectsCount() {
    if (!navProjectsCount) return;
    const seen = state.projectSeenRevision || {};
    const count = lastProjectRows.reduce((sum, row) => sum + (row._unread
        ? Math.max(0, (Number(row.visible_revision) || 0) - (Number(seen[row.id]) || 0)) : 0), 0);
    navProjectsCount.textContent = count ? (count > 99 ? '99+' : String(count)) : '';
    navProjectsCount.title = count ? `${count} unread message${count === 1 ? '' : 's'} in Projects` : '';
    if (count) navProjectsCount.setAttribute('aria-label', navProjectsCount.title);
    else navProjectsCount.removeAttribute('aria-label');
}

// Keep keyed controls (and menu anchors) when pinning reorders the list.
function paintProjectsNav() {
    const rows = hierarchyRows(lastProjectRows);
    const keyed = new Map([...navProjectsList.children].map(item => [item.dataset.navHierarchyKey || item.dataset.navProjectId, item]));
    const focused = document.activeElement;
    navProjects.hidden = false;
    paintProjectsCount();
    for (const [index, hierarchy] of rows.entries()) {
        const project = hierarchy.project;
        const deleting = String(project.lifecycle || 'active') === 'deleting';
        let item = keyed.get(hierarchy.key);
        keyed.delete(hierarchy.key);
        if (item && item.classList.contains('is-deleting') !== deleting) { item.remove(); item = null; }
        if (!item) {
            item = document.createElement('div');
            item.dataset.navProjectId = project.id;
            item.dataset.navHierarchyKey = hierarchy.key;
            item.className = `nav-project-item nav-project-tree${deleting ? ' is-deleting' : ''}`;
            const btn = document.createElement('button');
            btn.className = 'nav-row nav-project-row';
            btn.type = 'button';
            btn.dataset.projectId = project.id;
            const label = document.createElement('span');
            label.className = 'nav-row-label';
            btn.append(label, createStatusMark(document, 'nav-mark'));
            item.appendChild(btn);
            const branch = document.createElement('button');
            branch.type = 'button'; branch.className = 'nav-project-branch';
            branch.addEventListener('click', () => {
                const id = item.project.id;
                if (collapsedProjectBranches.has(id)) collapsedProjectBranches.delete(id); else collapsedProjectBranches.add(id);
                paintProjectsNav();
            });
            item.prepend(branch);
            btn.addEventListener('keydown', event => {
                if (event.key === 'ArrowRight' && item.hierarchy.hasChildren) {
                    event.preventDefault();
                    if (collapsedProjectBranches.has(item.project.id)) { collapsedProjectBranches.delete(item.project.id); paintProjectsNav(); }
                    else [...navProjectsList.children].find(n => !n.hidden && !n.hierarchy?.shortcut && parentId(n.project) === item.project.id)?.querySelector('.nav-project-row')?.focus();
                } else if (event.key === 'ArrowLeft' && !item.hierarchy.shortcut) {
                    event.preventDefault();
                    if (item.hierarchy.hasChildren && !collapsedProjectBranches.has(item.project.id)) { collapsedProjectBranches.add(item.project.id); paintProjectsNav(); }
                    else [...navProjectsList.children].find(n => !n.hierarchy?.shortcut && n.project?.id === parentId(item.project))?.querySelector('.nav-project-row')?.focus();
                }
            });
            if (deleting) {
                const status = document.createElement('span');
                status.className = 'nav-project-deleting-status';
                status.textContent = 'Closing…';
                item.appendChild(status);
            } else {
                // Pressing a row opens its Project; pressing the open one again keeps it open (like every reference).
                btn.addEventListener('click', () => openProjectPanel(item.project, { openOnly: true, composerFocus: true }));
                // The action control is a sibling button, never nested interactive UI.
                const kebab = document.createElement('button');
                kebab.type = 'button';
                kebab.className = 'nav-project-kebab';
                kebab.innerHTML = SHELL_ICONS.more;  // the shell's one ⋯ (page_icons.js)
                kebab.title = tr('project.kebab_title', 'Project actions');
                kebab.addEventListener('click', (event) => {
                    event.stopPropagation();
                    openProjectMenu(item, { anchor: kebab });
                });
                item.appendChild(kebab);
            }
        }
        item.project = project; item.hierarchy = hierarchy;
        item.toggleAttribute('data-pin-shortcut', Boolean(hierarchy.shortcut));
        item.toggleAttribute('data-pinned', Boolean(hierarchy.shortcut || (project.pinned === true && !parentId(project))));
        item.style.setProperty('--project-depth', Math.min(3, hierarchy.depth));
        const branch = item.querySelector('.nav-project-branch');
        branch.hidden = !hierarchy.hasChildren;
        branch.textContent = ''; 
        branch.setAttribute('aria-expanded', String(!collapsedProjectBranches.has(project.id)));
        branch.setAttribute('aria-label', `${collapsedProjectBranches.has(project.id) ? 'Expand' : 'Collapse'} children of ${project.name || project.id}`);
        branch.title = branch.getAttribute('aria-label');
        const btn = item.querySelector('.nav-project-row');
        btn.disabled = deleting;
        const label = btn.querySelector('.nav-row-label');
        label.textContent = `${hierarchy.depth > 3 ? `[${hierarchy.depth}] ` : ''}${project.name || project.id}`;
        if (hierarchy.shortcut || hierarchy.missingParent) {
            const context = document.createElement('span'); context.className = 'nav-project-context';
            context.textContent = hierarchy.shortcut ? `Shortcut · ${parentCaption(project, lastProjectRows)}` : `Parent not shown: ${parentId(project)}`;
            label.append(context);
        }
        btn.classList.toggle('active', project.id === navState.activeProjectId);
        const kebab = item.querySelector('.nav-project-kebab');
        if (kebab) kebab.setAttribute('aria-label', fmt('Actions for {name}', { name: project.name || project.id }));
        else item.querySelector('.nav-project-deleting-status').title = project.delete_error || 'Project closure is in progress; unfinished work is being stopped';
        if (navProjectsList.children[index] !== item) navProjectsList.insertBefore(item, navProjectsList.children[index] || null);
    }
    for (const item of keyed.values()) item.remove();
    // Moving an existing node can blur it even though its identity survives.
    if (focused?.isConnected && navProjectsList.contains(focused) && focused !== document.activeElement) focused.focus({ preventScroll: true });
    patchProjectActivityMarkers(projectActivityIndex);
}

// A Project row's one menu (project_create.js), from its ⋯ or by the row gesture bound once on the list below (a
// right-click, a long press, Shift+F10 or the ContextMenu key). It reads the row's Project as the menu opens, never a
// copy a closure kept, and opens with the row's mark and its words. Only the ⋯ says it has a popup. Says whether it opened.
function openProjectMenu(item, { anchor = null, point = null } = {}) {
    const project = lastProjectRows.find((row) => row.id === item?.dataset.navProjectId);
    if (!project || String(project.lifecycle || 'active') !== 'active') return false;
    const summary = projectActivityIndex.byChat.get(String(project.chat_id)) || IDLE_SUMMARY;
    const kebab = item.querySelector('.nav-project-kebab');
    const workItemProject = { id: project.id, name: project.name };
    return openProjectRowMenu(project, {
        apiClient, anchorEl: anchor || item.querySelector('.nav-project-row'), point,
        menuButton: anchor === kebab ? kebab : null, fallbackFocus: navProjectsToggle,
        status: { view: markView({ mark: summary.mark, owner: summary.owner, unread: project._unread }),
            word: summary.word, text: markWords(summary, project._unread) },
        isPinPending: () => pendingProjectPins.has(project.id),
        onPin: (pinned) => setProjectPin(project, pinned),
        onOpenBeside: () => openProjectPanel(project, { openOnly: true, besideSource: true }),
        onAddWorkItem: () => workItems.openCapture(workItemProject),
        onGoToWorkItems: () => goToProjectWorkItems(workItemProject),
        onChanged: (change = {}) => {
            if (change.optimistic && change.projectId) {
                const row = lastProjectRows.find(p => p.id === change.projectId);
                if (row) { row.lifecycle = 'deleting'; row._unread = false; }
                if (panes.keeps(change.projectId)) panes.closeOne(change.projectId);
                paintProjectsNav();
            } else refreshProjectsNav();
            knownProjectsJson = '';
        },
    });
}
const disposeProjectRowGesture = navProjectsList ? bindRowMenuGesture({
    root: navProjectsList,
    rowOf: (node) => {
        const item = node?.closest?.('.nav-project-item');
        return item && navProjectsList.contains(item) && !item.classList.contains('is-deleting') ? item : null;
    },
    open: openProjectMenu,
}) : () => {};

let workItemsNavigation = 0;
async function goToProjectWorkItems(project) {
    const request = ++workItemsNavigation;
    if (!await showPage('dashboard') || request !== workItemsNavigation || state.activePage !== 'dashboard') return;
    dashboardControls.activateTab('projects');
    void projectsManager.select(project.id);
    workItems.open(project, '', { listOnly: true });
}

// The ⋯ menu's Pin or Unpin asks for the state its label named, so a menu opened before
// another window changed the pin never flips it back. The row moves only once saved.
async function setProjectPin(project, pinned) {
    if (pendingProjectPins.has(project.id)) return;
    pendingProjectPins.add(project.id);
    const focused = document.activeElement;
    try {
        const { project: saved } = await apiClient.projectUpdate(project.id, { pinned });
        if (saved?.id !== project.id || saved.pinned !== pinned) throw new Error('Pin state was not confirmed');
        // Pre-response reads cannot overwrite this confirmation. The next forced
        // read shares the existing sequencer; only pinned is merged into the summary.
        stateSnapshots.retire();
        const row = lastProjectRows.find(p => p.id === project.id);
        if (row) row.pinned = saved.pinned;
        knownProjectsJson = '';
        renderProjectsNav(lastProjectRows, Array.from(state.projectChatIds));
        let keyboard = false;
        try { keyboard = focused?.matches(':focus-visible') === true; } catch { /* older selector support */ }
        if (keyboard && focused === document.activeElement && focused?.isConnected
                && focused.closest('.nav-project-item')?.dataset.navProjectId === project.id) {
            focused.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            const head = primarySidebar.querySelector('.sidebar-head');
            if (head && getComputedStyle(head).position === 'sticky') {
                const overlap = head.getBoundingClientRect().bottom - focused.getBoundingClientRect().top;
                if (overlap > 0) primarySidebar.scrollTop -= overlap;
            }
        }
    } catch (err) {
        showToast(fmt('Could not save project pin: {error}', { error: err?.body?.error || err.message }), 'error');
    } finally {
        pendingProjectPins.delete(project.id);
        repositionProjectRowMenu();
        refreshProjectsNav();
    }
}

async function createProjectFromClient() {
    const project = await openNewProjectDialog({
        apiClient,
        onCreated: () => { knownProjectsJson = ''; refreshProjectsNav(); },
    });
    if (project?.id) {
        // Fan-out learns the new thread immediately; then open its room.
        if (Number(project.chat_id)) state.projectChatIds.add(Number(project.chat_id));
        await refreshProjectsNav();
        openProjectPanel(project);
    }
}
document.getElementById('nav-projects-add')?.addEventListener('click', event => { event.stopPropagation(); void createProjectFromClient(); });

// Callers that must observe a read taken AFTER their own change (or after a
// socket open) force one, coalesced behind an in-flight read; the boot prefetch
// and the periodic poll join whatever page-wide read is in flight.
async function refreshProjectsNav(force = true) {
    const request = await stateSnapshots.gate(force);
    if (!request) return;
    try {
        const resp = await apiFetch(request.path || '/api/state', { cache: 'no-store' });
        if (!resp.ok) {
            stateSnapshots.fail(request);
            return;
        }
        const data = await resp.json();
        stateSnapshots.apply(request, data);
    } catch {
        stateSnapshots.fail(request);
    }
}

// A task bound to a project is ALREADY a project task. Main's chat instance owns
// what that means for its card (the card becomes the work's portal: chip, latest
// line, meta, its controls and the Project reference footer); it reads the same
// /api/state snapshot through hydrateStateSnapshot. The global stays for readers
// that only need the truthy gate.
function applyTaskBindings(bindings) {
    window.__ouroTaskBindings = bindings || {};
}

window.addEventListener('ouro:project-created', async (event) => {
    const project = event?.detail?.project;
    knownProjectsJson = '';
    await refreshProjectsNav();
    if (project?.id) {
        const resolved = lastProjectRows.find((item) => item.id === project.id) || project;
        openProjectPanel(resolved);
    }
});

// A converted task card's project-identity chip asks to open the project panel.
window.addEventListener('ouro:open-project', (event) => {
    const project = event?.detail?.project;
    if (!project?.id) return;
    const resolved = lastProjectRows.find((item) => item.id === project.id) || project;
    openProjectPanel(resolved, { openOnly: true, taskId: event.detail.task_id || '', quizId: event.detail.quiz_id || '' });
});

// A contribution's `open.tab` (contributions.js): the Widgets page, at that skill's first card once it is drawn.
window.addEventListener('ouro:open-widgets', async (event) => {
    const skill = String(event?.detail?.skill || '');
    const page = document.getElementById('page-widgets');
    if (!skill || !page || !await showPage('widgets')) return;
    const card = () => [...page.querySelectorAll('[data-widget-key]')].find((node) => node.dataset.widgetKey.startsWith(`${skill}:`));
    if (card()) return card().scrollIntoView({ block: 'nearest' });
    const observer = new MutationObserver(() => {
        if (!card()) return;
        observer.disconnect();
        card().scrollIntoView({ block: 'nearest' });
    });
    observer.observe(page, { childList: true, subtree: true });
    setTimeout(() => observer.disconnect(), 10000);  // a card that never comes leaves the page as it is
});

let disposePanelSizing = () => {};
function setupResizablePanels(prefs) {
    disposePanelSizing();
    disposePanelSizing = installPanelSizing(prefs, apiFetch);
}

apiFetch('/api/ui/preferences', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((prefs) => {
        mergeProjectSeenRevisions(prefs?.project_seen_revision);
        mergeMainSeenRevision(prefs);
        observeMainRevision(null);
        setupResizablePanels(prefs || {});
        // Re-evaluate unread now that revision cursors are known.
        if (Array.isArray(lastProjectRows)) { knownProjectsJson = null; renderProjectsNav(lastProjectRows, Array.from(state.projectChatIds || [])); }
    })
    .catch(() => setupResizablePanels({}));

// The interface language is an install-wide setting, not a UI preference: the gateway
// answers the chosen tag and the memory the overlay paints from at /api/ui/i18n. Other
// clients learn about a change from the frames below; the settings save path broadcasts
// nothing, so these are the only cross-client signals.
markBootRead(apiClient.uiI18n()
    .then((i18n) => setLanguage(i18n.language, i18n).then(() => i18n))
    .catch(() => setLanguage(storedLanguage()).then(() => null)));
ws.on('ui_language_changed', () => { refreshDictionary(); });
ws.on('ui_i18n_updated', () => { refreshDictionary(); });

ws.on('open', () => {
    activitySocketDisconnected = false;
    stateSnapshots.retire();
    markProjectActivityUnknown();
    refreshProjectsNav(true); // the in-flight read predates the socket: one coalesced post-open read
    pendingBootRead().then(refreshDictionary); // every open, the first too: a frame sent before this subscription was missed (behind the boot read, whose older answer must not land last)
    loadVersion(); // a page the reload decision keeps may now face a core of another version
});
ws.on('close', () => {
    activitySocketDisconnected = true;
    stateSnapshots.retire();
    markProjectActivityUnknown();
});
// A backend-created project (e.g. the agent's promote_chat_to_task tool) pushes
// this so the live WS fan-out learns the new project chat_id immediately, instead
// of waiting for the periodic poll and misrouting early frames into the main chat.
// Add the chat_id SYNCHRONOUSLY from the payload so fan-out is correct before the
// async /api/state refresh returns; then refresh the full nav/list.
ws.on('projects_changed', (msg) => {
    const cid = Number(msg && msg.chat_id) || 0;
    if (cid) state.projectChatIds.add(cid);
    refreshProjectsNav();
});
setInterval(() => refreshProjectsNav(false), 20000);
settingsControls = initSettings(ctx);
dashboardControls = initDashboard(ctx);
const projectsManager = initProjectsManager({ mount: document.getElementById('dashboard-panel-projects'), state,
    onCreate: createProjectFromClient,
    onWorkItems: project => workItems.open(project, '', { dashboard: true }),
    onSelection: project => workItems.followProject(project),
    onStorage: () => { dashboardControls.activateTab('storage'); document.getElementById('dashboard-tab-storage')?.focus(); },
    onOpen: (project, beside) => openProjectPanel(project, { openOnly: true, besideSource: beside, composerFocus: !beside }),
    onChanged: () => { knownProjectsJson = ''; return refreshProjectsNav(); },
    onPin: (project, pinned) => setProjectPin(project, pinned),
});
initLogs({ ...ctx, mount: document.getElementById('dashboard-panel-logs') });
evolution = initEvolution({ ...ctx, mount: document.getElementById('dashboard-panel-evolution') });
initUpdates({ ...ctx, mount: document.getElementById('dashboard-panel-updates') });
initActivity({ ...ctx, mount: document.getElementById('dashboard-panel-activity') });
initConsciousnessDashboard({ ...ctx, mount: document.getElementById('dashboard-panel-consciousness') });
initCosts({ ...ctx, mount: document.getElementById('dashboard-panel-costs') });
const disposeStorage = initStorage({ ...ctx, mount: document.getElementById('dashboard-panel-storage') });
initSkills(ctx);
initWidgets(ctx);
const updateStatus = initUpdateStatus(ctx);
// The interface size's one controller: the desktop keys work on every page, the slider lives in Settings.
getInterfaceSize();

initOnboardingOverlay();

const disposeScrollFades = Array.from(document.querySelectorAll('.scroll-fade-y'), bindScrollFade);
// Inside Telegram the host's chrome follows the shell (host_telegram.js); a no-op elsewhere.
const telegramHost = createTelegramHost({ layers });
telegramHost.bind();
window.addEventListener('pagehide', (event) => {
    if (event.persisted) return;
    evolution.dispose();
    updateStatus.dispose();
    disposeStorage?.destroy();
    projectsManager.destroy();
    workItems.destroy();
    disposeScrollFades.forEach((dispose) => dispose());
    telegramHost.dispose();
    disposeDrawerDrag();
    disposeProjectRowGesture();
    panes.destroy();
    disposePanelSizing();
    disposeMobileKeyboard();
    for (const inst of projectInstances.values()) inst.destroy();
});
loadVersion();
syncNavigationState();
const hashPage = pageFromHash();
if (hashPage && hashPage !== state.activePage) showPage(hashPage);

const mobileKeyboard = installMobileKeyboard();
releaseMobileKeyboardForDrawer = mobileKeyboard.releaseForDrawer;
disposeMobileKeyboard = mobileKeyboard.dispose;

// Windows Alt / Layout Switch Menu-lock suppression (shared installer — the
// onboarding wizard document installs the same guard on its own iframe window).
installAltMenuSuppression();

// Desktop-shell link parity: one delegated interceptor + window.open shim that
// routes target="_blank"/download/data:/blob: intents over the pywebview
// bridge. Installs only when the bridge announces itself; inert in browsers.
installDesktopShellLinkInterceptor();

// Populate the project-thread isolation set BEFORE opening the socket so the live
// fan-out never misclassifies an early project frame as main-chat traffic during
// startup (chat.js::isMyThread relies on state.projectChatIds). Connect even if
// the prefetch fails, then ws.on('open') keeps it fresh.
refreshProjectsNav(false).finally(() => ws.connect());

window.addEventListener('viper:focus-document-room', async event => {
 const id = event.detail.projectId;
 if (id) { const row = lastProjectRows.find(p => p.id === id); if (row) await openProjectPanel(row, { openOnly: true, besideSource: !panes.keeps(id) && panes.collaborating() }); }
 else { if (!await showPage('chat')) return; panes.focusMain(); }
 (projectInstances.get(id) || mainChat)?.composer?.focus();
});
window.addEventListener('viper:reader-closed', () => { for (const [id, inst] of projectInstances) if (!panes.keeps(id) && !inst.hasPendingWork()) destroyProjectInstance(id); });
