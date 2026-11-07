/** Standalone-message revisions, never OS notification clicks or task activity.
 * Descendants are only those in the bounded supplied sidebar view. No reads/ACKs. */
export function unreadCount(row, cursors = {}) {
    if (String(row.lifecycle || 'active') !== 'active') return 0;
    const revision = row.visible_revision;
    const seen = Object.hasOwn(cursors, row.id) ? cursors[row.id] : 0;
    if (!Number.isSafeInteger(revision) || revision < 0 || !Number.isSafeInteger(seen) || seen < 0) return null;
    return Math.max(0, revision - seen);
}

export function descendantUnread(rows, cursors = {}) {
    const byId = new Map(rows.map(row => [row.id, row])), result = new Map();
    for (const row of byId.values()) {
        const count = unreadCount(row, cursors);
        if (!(count > 0)) continue;
        const visited = new Set([row.id]);
        let parent = row.parent_id;
        while (parent && byId.has(parent) && !visited.has(parent)) {
            visited.add(parent);
            if (!result.has(parent)) result.set(parent, []);
            result.get(parent).push({ id: row.id, name: row.name || row.id, count });
            parent = byId.get(parent).parent_id;
        }
    }
    return result;
}

// Global collapse keeps unread destinations and their known paths discoverable.
// A deliberate branch collapse still hides its children; the ancestor's cue names them.
export function activeUnreadPath(rows, id) {
    const byId = new Map(rows.map(row => [row.id, row])), result = new Set();
    while (id && byId.has(id) && !result.has(id)) { result.add(id); id = byId.get(id).parent_id; }
    return result;
}

export function unreadVisible(hierarchy, collapsedBranches, expanded, descendants, cursors, usual, activePath = new Set()) {
    if (usual || expanded) return usual;
    if (hierarchy.ancestors.some(id => collapsedBranches.has(id))) return false;
    if (hierarchy.project.pinned === true && hierarchy.project.parent_id) return false; // Its shortcut is already visible.
    return activePath.has(hierarchy.project.id) || unreadCount(hierarchy.project, cursors) > 0 || Boolean(descendants.get(hierarchy.project.id)?.length);
}

export function paintUnread(button, row, cursors, descendants) {
    let region = button.querySelector('.nav-project-unread');
    if (!region) {
        region = button.ownerDocument.createElement('span'); region.className = 'nav-project-unread';
        const own = button.ownerDocument.createElement('span'); own.className = 'nav-project-unread-own';
        const nested = button.ownerDocument.createElement('span'); nested.className = 'nav-project-unread-descendants';
        region.append(own, nested); button.append(region);
    }
    const count = unreadCount(row, cursors), children = descendants.get(row.id) || [];
    const own = region.firstElementChild, nested = region.lastElementChild;
    own.textContent = count > 0 ? String(count) : ''; own.hidden = !(count > 0);
    const words = count === null ? 'Unread message count unavailable' : count > 0
        ? `${count} unread message${count === 1 ? '' : 's'} in ${row.name || row.id}` : '';
    own.title = words;
    nested.replaceChildren(); nested.hidden = children.length === 0;
    if (children.length) {
        const svg = button.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('width', '14'); svg.setAttribute('height', '14');
        const path = button.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M3 2v8h10m-3-3 3 3-3 3'); path.setAttribute('fill', 'none');
        path.setAttribute('stroke', 'currentColor'); path.setAttribute('stroke-width', '1.5');
        svg.append(path); nested.append(svg);
    }
    const childWords = children.length ? `Unread in listed descendants: ${children.map(child => `${child.name} (${child.count})`).join(', ')}` : '';
    nested.title = childWords;
    // The full words are also appended to the row's accessible name by its existing painter.
    region.setAttribute('aria-hidden', 'true'); region.hidden = own.hidden && nested.hidden;
    button.dataset.unreadCount = count === null ? 'unknown' : String(count);
    return [words, childWords].filter(Boolean).join(' · ');
}
