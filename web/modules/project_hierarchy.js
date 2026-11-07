/** Logical room organization only. Never resolves work, resources, permissions or panes. */
export const parentId = project => typeof project?.parent_id === 'string' ? project.parent_id : '';

// Incoming rows are an explicitly bounded server view. A missing parent is NOT a root claim.
// Iterative traversal has no depth cap and remains readable even with malformed cyclic input.
export function hierarchyRows(projects) {
    const byId = new Map(projects.map(p => [p.id, p]));
    const children = new Map();
    for (const project of projects) {
        const parent = parentId(project);
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(project);
    }
    const visited = new Set(), result = [];
    function walk(roots) {
        const stack = roots.slice().reverse().map(project => ({ project, ancestors: [] }));
        while (stack.length) {
            const { project, ancestors } = stack.pop();
            if (visited.has(project.id)) continue;
            visited.add(project.id);
            result.push({ project, key: project.id, ancestors, depth: ancestors.length,
                hasChildren: Boolean(children.get(project.id)?.length), missingParent: Boolean(parentId(project) && !byId.has(parentId(project))) });
            const nested = children.get(project.id) || [];
            for (let i = nested.length - 1; i >= 0; --i) stack.push({ project: nested[i], ancestors: [...ancestors, project.id] });
        }
    }
    walk(projects.filter(p => !parentId(p) || !byId.has(parentId(p))));
    walk(projects.filter(p => !visited.has(p.id))); // Preserve malformed rows without looping or inventing ancestry.
    const shortcuts = projects.filter(p => p.pinned === true && parentId(p)).map(project => ({
        project, key: `pin:${project.id}`, ancestors: [], depth: 0, hasChildren: false, shortcut: true,
    }));
    return [...shortcuts, ...result];
}

export function hierarchyVisible(row, collapsed, projectsExpanded) {
    if (!projectsExpanded) return row.shortcut || (row.project.pinned === true && !parentId(row.project));
    return row.shortcut || !row.ancestors.some(id => collapsed.has(id));
}

export function parentCaption(project, known = []) {
    const id = parentId(project);
    if (!id) return 'Top level';
    const parent = known.find(p => p.id === id);
    return `Parent: ${parent?.name || id}`;
}

// The full catalog is not required for safety: server validation is authoritative.
// Exclude only descendants whose relationship is actually known in this view.
export function knownDescendants(id, projects) {
    const result = new Set([id]);
    const children = new Map();
    for (const p of projects) { const parent = parentId(p); if (!children.has(parent)) children.set(parent, []); children.get(parent).push(p.id); }
    const stack = [id];
    while (stack.length) for (const child of children.get(stack.pop()) || []) if (!result.has(child)) { result.add(child); stack.push(child); }
    return result;
}
