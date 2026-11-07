/** A modeless reader's selection belongs to its originating composer, never the active room. */
export function bindReaderCollaboration(dialog, { send, doc = document }) {
    const body = dialog.querySelector('.document-reader-body');
    const button = doc.createElement('button');
    button.type = 'button'; button.className = 'btn btn-default viper-selection-menu';
    button.textContent = 'Send to chat'; button.hidden = true;
    dialog.append(button);
    let selected = '';
    const hide = () => { button.hidden = true; };
    const context = event => {
        if (event.shiftKey || typeof send !== 'function') return; // native menu when no chat destination exists
        const selection = doc.getSelection();
        if (!selection?.rangeCount || !body.contains(selection.anchorNode) || !body.contains(selection.focusNode) || selection.isCollapsed) return;
        selected = selection.toString();
        if (!selected) return;
        event.preventDefault();
        const rect = dialog.getBoundingClientRect();
        button.style.setProperty('--selection-x', `${Math.max(4, Math.min(rect.width - 150, event.clientX - rect.left))}px`);
        button.style.setProperty('--selection-y', `${Math.max(4, Math.min(rect.height - 44, event.clientY - rect.top))}px`);
        button.hidden = false; button.focus({ preventScroll: true });
    };
    const click = event => { if (!button.contains(event.target)) hide(); };
    const key = event => {
        if (event.key !== 'Escape' || button.hidden || event.isComposing) return;
        event.preventDefault(); event.stopPropagation(); hide(); body.focus({ preventScroll: true });
    };
    dialog.addEventListener('keydown', key);
    const submit = () => { const text = selected; hide(); if (text) send(text + ' :: '); };
    body.addEventListener('contextmenu', context);
    button.addEventListener('click', submit);
    button.addEventListener('keydown', key);
    doc.addEventListener('pointerdown', click);
    // Drag the header, not its selectable title or controls; the document and its scroll node never remount.
    const header = dialog.querySelector('.document-reader-head');
    let drag = null;
    const down = event => {
        if (innerWidth <= 620 || event.button !== 0 || event.target.closest('button, a, .document-reader-identity')) return;
        const rect = dialog.getBoundingClientRect(); drag = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
        header.setPointerCapture(event.pointerId);
    };
    const move = event => {
        if (!drag) return;
        dialog.style.setProperty('--reader-left', `${Math.max(0, Math.min(innerWidth - 80, drag.left + event.clientX - drag.x))}px`);
        dialog.style.setProperty('--reader-top', `${Math.max(0, Math.min(innerHeight - 60, drag.top + event.clientY - drag.y))}px`);
    };
    const up = () => { drag = null; };
    header.addEventListener('pointerdown', down); header.addEventListener('pointermove', move);
    header.addEventListener('pointerup', up); header.addEventListener('lostpointercapture', up);
    return () => {
        body.removeEventListener('contextmenu', context); button.removeEventListener('click', submit);
        button.removeEventListener('keydown', key); dialog.removeEventListener('keydown', key); doc.removeEventListener('pointerdown', click);
        header.removeEventListener('pointerdown', down); header.removeEventListener('pointermove', move);
        header.removeEventListener('pointerup', up); header.removeEventListener('lostpointercapture', up); button.remove();
    };
}
