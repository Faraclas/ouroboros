/** Explicit Work Item destinations; parsing is local and never discovers an association. */
const ITEM_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function workItemHref(id) {
    return typeof id === 'string' && ITEM_ID.test(id) ? `/?work_item=${id.toLowerCase()}` : '';
}

/** Only this installation's root, one work_item parameter, and no other intent. */
export function workItemId(value, base = 'https://work-item.invalid/') {
    if (typeof value !== 'string' || /[\\\s\u0000-\u001f\u007f]/.test(value)) return '';
    if (!value.startsWith('/?') && !/^https?:\/\//i.test(value)) return '';
    try {
        const url = new URL(value, base), origin = new URL(base).origin;
        const params = [...url.searchParams];
        if (url.origin !== origin || url.username || url.password || url.pathname !== '/' || url.hash
            || params.length !== 1 || params[0][0] !== 'work_item' || !ITEM_ID.test(params[0][1])) return '';
        return params[0][1].toLowerCase();
    } catch { return ''; }
}

/** One click owner survives streamed/replayed Markdown; modified clicks keep ordinary new-tab behavior. */
export function bindWorkItemReferences({ doc = document, win = window, openItem, onError } = {}) {
    let pending = null, pendingAnchor = null;
    const lifetime = new AbortController();
    const activate = async (id, anchor = null) => {
        pending?.abort();
        pendingAnchor?.removeAttribute('aria-busy');
        const own = pending = new AbortController();
        pendingAnchor = anchor;
        const cancel = () => own.abort();
        // A later gesture is a new intent; a slow read may not replace its view.
        for (const type of ['pointerdown', 'keydown']) doc.addEventListener(type, cancel, { capture: true, signal: own.signal });
        anchor?.setAttribute('aria-busy', 'true');
        try { await openItem(id, { signal: own.signal }); }
        catch (error) { if (!own.signal.aborted) onError?.(error); }
        finally {
            own.abort();
            if (pending === own) { anchor?.removeAttribute('aria-busy'); pending = null; pendingAnchor = null; }
        }
    };
    doc.addEventListener('click', event => {
        if (event.defaultPrevented || event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const anchor = event.target?.closest?.('a[href]');
        if (!anchor || anchor.hasAttribute('download')) return;
        const id = workItemId(anchor.getAttribute('href'), win.location.href);
        if (!id) return;
        event.preventDefault();
        void activate(id, anchor);
    }, { capture: true, signal: lifetime.signal });
    // A copied link/new tab has the same destination, with no board read on ordinary startup.
    const initial = workItemId(win.location.href, win.location.href);
    if (initial) void activate(initial);
    return () => { lifetime.abort(); pending?.abort(); pendingAnchor?.removeAttribute('aria-busy'); pending = null; pendingAnchor = null; };
}
