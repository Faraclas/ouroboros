/** Capture only one message's selected words, before menu/button focus can collapse them. */
export function messageSelection(row, doc = row?.ownerDocument) {
    const selection = doc?.getSelection?.();
    const message = row?.querySelector(':scope > .message');
    if (!message || !selection || selection.rangeCount !== 1 || selection.isCollapsed
        || !message.contains(selection.anchorNode) || !message.contains(selection.focusNode)) return '';
    return selection.toString();
}

/** A selection is ordinary editable draft text, not an invented host quote/snippet. Never sends. */
export function appendSelection(composer, text) {
    if (!composer || !text) return;
    const draft = composer.getDraft();
    composer.setDraft(draft + (draft && !draft.endsWith('\n') ? '\n' : '') + text + ' :: ');
    composer.focus();
    const end = composer.getDraft().length;
    composer.input.setSelectionRange(end, end);
}
