/** The shell's layout facts, read through one door.
 *
 * `web/theme.js` owns the switch: it runs before CSS, so the first paint is already the
 * right shell, writes `html[data-layout="docked"|"stack"]`, keeps it current on resize and
 * rotation, and publishes its number once as `window.ouroLayout`. Everything else
 * (app.js, client_surface.js, the layer stack, the drawer drag) asks this module and never
 * writes a media query of its own; the shell's CSS keys off the attribute
 * (tests/test_shell_layout_static.py). */

export const LAYOUTS = Object.freeze(['docked', 'stack']);

const win = () => globalThis.window ?? globalThis;
const rootOf = (doc = globalThis.document) => doc?.documentElement ?? null;

/** The published number and the current layout; theme.js must have run. */
export function layoutFacts(w = win()) {
    const facts = w?.ouroLayout;
    if (!facts) throw new Error('web/theme.js has not published window.ouroLayout');
    return facts;
}

/** `docked` or `stack`: the attribute theme.js painted, else what it resolves right now; a document with
 * neither (a test's fake DOM) is a window, so docked, as theme.js itself falls back. */
export function currentLayout({ root = rootOf(), w = win() } = {}) {
    const painted = root?.dataset?.layout;
    if (LAYOUTS.includes(painted)) return painted;
    const resolved = w?.ouroLayout?.layout;
    return LAYOUTS.includes(resolved) ? resolved : 'docked';
}

export function isStackLayout(options) {
    return currentLayout(options) === 'stack';
}

/** A pinch is magnifying the page: the visual viewport is narrower than the layout one, the root's client width
 * (`innerWidth` runs a pixel past the screen at an Interface size below 100 % and counts a classic scrollbar). A
 * keyboard only takes height, and a page zoom moves both together, so neither counts. While it holds, the room keeps its
 * size and the finger pans the magnified page (DESIGN "Touch and containment"). */
export function pinchMagnified(w = win()) {
    const vv = w?.visualViewport;
    const layoutWidth = Number(w?.document?.documentElement?.clientWidth) || 0;
    return Boolean(vv && layoutWidth && Number(vv.width) < layoutWidth - 1);
}

/** A window too short for the sidebar's zones pins its head and its foot over the one scroller (style.css). Their
 * measured heights are the scroll margin of the rows there, so a row reached with Tab or brought into view is never
 * left under either bar. Their border boxes are watched: the macOS title band and the safe area are their padding
 * and can arrive after the first paint (a Telegram inset, a page zoom), which a content box never reports. Returns
 * the disposer. */
export function measurePinnedBars(sidebar, { Observer = globalThis.ResizeObserver } = {}) {
    const head = sidebar?.querySelector('.sidebar-head');
    const foot = sidebar?.querySelector('.sidebar-foot-meta');
    if (!head || !foot || typeof Observer !== 'function') return () => {};
    const write = () => {
        sidebar.style.setProperty('--sidebar-head-h', `${head.getBoundingClientRect().height}px`);
        sidebar.style.setProperty('--sidebar-foot-h', `${foot.getBoundingClientRect().height}px`);
    };
    const observer = new Observer(write);
    observer.observe(head, { box: 'border-box' });
    observer.observe(foot, { box: 'border-box' });
    write();
    return () => observer.disconnect();
}

/** Hear the switch move; returns the disposer. The handler gets the new layout name. */
export function onLayoutChange(handler, { w = win() } = {}) {
    const listener = (event) => handler(event?.detail?.layout || currentLayout({ w }));
    w.addEventListener('ouro:layout-changed', listener);
    return () => w.removeEventListener('ouro:layout-changed', listener);
}


// Resizable side sections: edge drag-handles resize the sidebar and the Project panel, whose widths live in
// --sidebar-width / --project-panel-width on :root, persisted (debounced) to /api/ui/preferences.
// Disabled in the stack layout (layout.js). Width 0 = keep the CSS default.
// While a handle is dragged, each frame writes the measured width into the dragged box's own --drag-width, which no
// element inherits (style.css), and everything that lines up with the box follows it by layout: one element
// restyles. The stored width on :root is inherited by every element, so writing it per frame restyled the whole
// document, about half of a drag frame on the owner's Main in WebKit; it is written once, when the drag ends.
// CW10 note: the DEVELOPMENT.md "no inline styles in JS" rule targets static styling
// that belongs in a stylesheet — the drag's transient `userSelect:none` was that, and
// is now the `.resizing-panels` class. A measured width in a narrowly named custom property is the exception
// DEVELOPMENT "Design System" names.
export function installPanelSizing(prefs, apiFetch) {
    const root = document.documentElement;
    const lifetime = new AbortController();
    const cleanups = [];
    let persistTimer = 0;
    const persist = (patch) => {
        clearTimeout(persistTimer);
        persistTimer = setTimeout(() => {
            apiFetch('/api/ui/preferences', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            }).catch(() => {});
        }, 400);
    };
    if (Number(prefs?.sidebar_width) > 0) root.style.setProperty('--sidebar-width', `${prefs.sidebar_width}px`);
    if (Number(prefs?.project_panel_width) > 0) root.style.setProperty('--project-panel-width', `${prefs.project_panel_width}px`);
    const makeHandle = (target, cssVar, dir, prefKey, min, max) => {
        if (!target) return;
        const handle = document.createElement('div');
        handle.className = `resize-handle resize-handle-${dir}`;
        handle.title = 'Drag to resize';
        target.appendChild(handle);
        let startX = 0, startW = 0, dragging = false, frame = 0, wanted = 0;
        cleanups.push(() => { if (frame) cancelAnimationFrame(frame); target.style.removeProperty('--drag-width'); handle.remove(); });
        // One write per displayed frame (a mouse reports more often than the screen draws), on the dragged box only.
        const apply = () => {
            frame = 0;
            target.style.setProperty('--drag-width', `${wanted}px`);
        };
        handle.addEventListener('pointerdown', (e) => {
            if (isStackLayout()) return;  // the stack layout has a drawer, not a resizable column
            dragging = true; startX = e.clientX; startW = target.getBoundingClientRect().width;
            try { handle.setPointerCapture(e.pointerId); } catch {}
            document.body.classList.add('resizing-panels');  // CW10: class, not inline style
            e.preventDefault();
        }, { signal: lifetime.signal });
        handle.addEventListener('pointermove', (e) => {
            if (!dragging) return;
            const delta = dir === 'right' ? (e.clientX - startX) : (startX - e.clientX);
            wanted = Math.max(min, Math.min(max, Math.round(startW + delta)));
            if (!frame) frame = requestAnimationFrame(apply);
        }, { signal: lifetime.signal });
        const end = (e) => {
            if (!dragging) return;
            if (frame) { cancelAnimationFrame(frame); apply(); }
            dragging = false; document.body.classList.remove('resizing-panels');  // CW10
            try { handle.releasePointerCapture(e.pointerId); } catch {}
            // Keep the width that is shown: a Project panel dragged past the content area's edge stops at it. A box
            // hidden mid-drag (Escape closed the Project) shows no width, and the stored one stays.
            const shown = Math.round(target.getBoundingClientRect().width) || 0;
            if (shown) {
                root.style.setProperty(cssVar, `${shown}px`);
                persist({ [prefKey]: shown });
            }
            target.style.removeProperty('--drag-width');
        };
        handle.addEventListener('pointerup', end, { signal: lifetime.signal });
        handle.addEventListener('pointercancel', end, { signal: lifetime.signal });
    };
    makeHandle(document.getElementById('primary-sidebar'), '--sidebar-width', 'right', 'sidebar_width', 180, 560);
    makeHandle(document.getElementById('project-panel'), '--project-panel-width', 'left', 'project_panel_width', 320, 1100);
    return () => { lifetime.abort(); clearTimeout(persistTimer); cleanups.forEach(dispose => dispose()); document.body.classList.remove('resizing-panels'); };
}


// Mobile soft-keyboard handling: viewport shrink counts only while an editable
// owns focus. Drawer opening clears that state explicitly before navigation is
// rendered, so stale WebView geometry cannot hide an otherwise-open sidebar.
export function installMobileKeyboard() {
    const lifetime = new AbortController();
    const vvhStyle = document.createElement('style');
    vvhStyle.id = 'runtime-vvh';
    document.head.appendChild(vvhStyle);

    const keyboardEditableSelector = [
        'textarea',
        'select',
        'input:not([type])',
        'input[type="text"]',
        'input[type="search"]',
        'input[type="email"]',
        'input[type="url"]',
        'input[type="tel"]',
        'input[type="password"]',
        'input[type="number"]',
        '[contenteditable]:not([contenteditable="false"])',
    ].join(',');

    let keyboardOpen = false;
    let keyboardTouchStartY = 0;
    let stableViewportHeight = 0;
    let focusBaselineHeight = 0;
    let focusRevision = 0;
    let baselineFrame = 0;

    function keyboardEditable(node) {
        if (!(node instanceof Element)) return null;
        const editable = node.closest(keyboardEditableSelector);
        if (!editable) return null;
        if (editable.matches('input, textarea, select') && (editable.disabled || editable.readOnly)) {
            return null;
        }
        return editable;
    }

    function focusedKeyboardEditable() {
        return keyboardEditable(document.activeElement);
    }

    function viewportHeight() {
        const candidates = [
            window.visualViewport?.height,
            window.innerHeight,
            document.documentElement.clientHeight,
        ];
        return Number(candidates.find((value) => Number.isFinite(value) && value > 0) || 0);
    }

    function findScrollableKeyboardNode(target) {
        let el = target;
        while (el && el !== document.body) {
            const overflow = getComputedStyle(el).overflowY;
            if (['auto', 'scroll'].includes(overflow) && el.scrollHeight > el.clientHeight) return el;
            el = el.parentElement;
        }
        return null;
    }

    function lockTouchStart(e) {
        if (e.touches && e.touches.length) keyboardTouchStartY = e.touches[0].clientY;
    }

    function lockBoundaryTouch(e) {
        const touch = e.touches && e.touches.length ? e.touches[0] : null;
        const scrollable = findScrollableKeyboardNode(e.target);
        if (scrollable && touch) {
            const dy = touch.clientY - keyboardTouchStartY;
            const atTop = scrollable.scrollTop <= 0;
            const atBottom = Math.ceil(scrollable.scrollTop + scrollable.clientHeight) >= scrollable.scrollHeight;
            if ((!atTop && dy > 0) || (!atBottom && dy < 0)) return;
        }
        e.preventDefault();
    }

    function applyKeyboardState(visible) {
        const nextVisible = Boolean(visible);
        if (nextVisible && !keyboardOpen) {
            window.scrollTo(0, 0);
            document.addEventListener('touchstart', lockTouchStart, { passive: true });
            document.addEventListener('touchmove', lockBoundaryTouch, { passive: false });
        } else if (!nextVisible && keyboardOpen) {
            document.removeEventListener('touchstart', lockTouchStart);
            document.removeEventListener('touchmove', lockBoundaryTouch);
        }
        document.documentElement.classList.toggle('keyboard-open', nextVisible);
        document.body.classList.toggle('keyboard-open', nextVisible);
        keyboardOpen = nextVisible;
    }

    function cancelBaselineFrame() {
        if (!baselineFrame) return;
        cancelAnimationFrame(baselineFrame);
        baselineFrame = 0;
    }

    function rememberStableViewport(height) {
        cancelBaselineFrame();
        const revision = focusRevision;
        baselineFrame = requestAnimationFrame(() => {
            baselineFrame = 0;
            if (revision !== focusRevision || focusedKeyboardEditable()) return;
            stableViewportHeight = height;
        });
    }

    const updateVvh = () => {
        const h = viewportHeight();
        if (isStackLayout()) {
            // A pinch only magnifies: the room keeps the height it had, or it would shrink with the visual viewport.
            if (pinchMagnified()) return;
            vvhStyle.textContent = ':root{--vvh:' + Math.max(320, Math.ceil(h)) + 'px}';
            if (!focusedKeyboardEditable()) {
                applyKeyboardState(false);
                rememberStableViewport(h);
                return;
            }
            cancelBaselineFrame();
            if (!focusBaselineHeight) focusBaselineHeight = stableViewportHeight || h;
            const shrink = focusBaselineHeight - h;
            applyKeyboardState(shrink > Math.max(120, focusBaselineHeight * 0.25));
            return;
        }
        cancelBaselineFrame();
        applyKeyboardState(false);
        stableViewportHeight = h;
        focusBaselineHeight = 0;
        vvhStyle.textContent = ':root{--vvh:100dvh}';
    };

    document.addEventListener('focusin', (event) => {
        if (!keyboardEditable(event.target)) return;
        focusRevision += 1;
        cancelBaselineFrame();
        focusBaselineHeight = stableViewportHeight || viewportHeight();
        updateVvh();
    }, { signal: lifetime.signal });
    document.addEventListener('focusout', (event) => {
        if (!keyboardEditable(event.target)) return;
        focusRevision += 1;
        const revision = focusRevision;
        cancelBaselineFrame();
        requestAnimationFrame(() => {
            if (revision !== focusRevision || focusedKeyboardEditable()) return;
            applyKeyboardState(false);
            focusBaselineHeight = 0;
        });
    }, { signal: lifetime.signal });

    const releaseForDrawer = () => {
        const editable = focusedKeyboardEditable();
        if (editable && typeof editable.blur === 'function') editable.blur();
        applyKeyboardState(false);
    };

    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', updateVvh, { signal: lifetime.signal });
        window.visualViewport.addEventListener('scroll', updateVvh, { signal: lifetime.signal });
    }
    window.addEventListener('resize', updateVvh, { signal: lifetime.signal });
    stableViewportHeight = viewportHeight();
    updateVvh();
    return { releaseForDrawer, dispose() {
        focusRevision += 1; lifetime.abort(); cancelBaselineFrame(); applyKeyboardState(false); vvhStyle.remove();
    } };
}

