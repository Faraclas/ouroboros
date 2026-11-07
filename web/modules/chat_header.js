/** Room bar (DESIGN §10): status, consciousness and Panic; Main adds Restart and
 * Copilot usage, whose Costs door retains App safety budget. A narrow bar moves
 * Restart/usage into the status Shade with the same nodes and handlers. */
import { bindCopilotHeader } from './copilot_usage.js';
import { confirmAndSendRestart, sendPanic } from './chat_activity.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { createConsciousnessStatus } from './consciousness_status.js';
import { bindHeaderControlPlacement } from './page_header.js';

/** Panic: immediate in every room's bar, never behind a menu or a save. */
export function panicButtonHtml() {
    return '<button class="chat-header-btn danger ui-chrome-control" type="button" data-chat-command="panic" title="Stop all workers">Panic</button>';
}

export function mainHeaderActionsHtml() {
    return `
                <div class="chat-header-actions" id="chat-header-actions">
                    <button class="chat-header-btn ui-chrome-control" type="button" data-chat-command="restart" title="Restart agent">Restart</button>
                    ${panicButtonHtml()}
                </div>
                <button class="chat-budget-pill ui-chrome-control" id="chat-budget-pill" type="button" title="Checking GitHub Copilot usage" aria-label="Checking GitHub Copilot usage">
                    <span class="chat-budget-word">Copilot</span>
                    <span class="chat-budget-text" id="chat-budget-text">—</span><svg class="copilot-usage-stale" aria-hidden="true" hidden viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>
                    <div class="chat-budget-bar" role="progressbar" aria-label="Copilot account percentage used" aria-valuemin="0" aria-valuemax="100">
                        <div class="chat-budget-bar-fill" id="chat-budget-bar-fill"></div>
                    </div>
                </button>
                <span id="chat-status" class="status-badge offline">Connecting…</span>
            `;
}

/**
 * Wire Restart/Panic, the Copilot usage/Costs door and consciousness; Main's
 * `shade` (room_work.js) receives Restart/usage when the bar is narrow.
 * `update(data)` takes every `/api/state` answer the room reads. Returns `{ update, destroy }`.
 */
export function createRoomHeader({ bar, ws, shade = null, openDashboardTab = null, refreshState = () => {} }) {
    if (!bar) return null;
    const actions = bar.querySelector('.chat-header-actions');
    const budget = bar.querySelector('.chat-budget-pill');
    const onCommand = async (event) => {
        const command = event.currentTarget?.dataset.chatCommand || event.target?.dataset?.chatCommand;
        if (command === 'restart') await confirmAndSendRestart({ openConfirmDialog, ws });
        else if (command === 'panic') sendPanic({ ws });
    };
    const commands = [...bar.querySelectorAll('[data-chat-command]')];
    commands.forEach((button) => button.addEventListener('click', onCommand));
    const onBudget = () => openDashboardTab?.('costs');
    budget?.addEventListener('click', onBudget);
    const disposeCopilot = bindCopilotHeader(budget);
    const disposePlacement = actions && shade ? bindHeaderControlPlacement({ headerActions: actions, budgetPill: budget, shade }) : () => {};
    const consciousness = createConsciousnessStatus({ bar, ws, openDashboardTab, refreshState });
    return {
        update(data) {
            consciousness?.update(data);
            // Copilot owns its bounded shared read; app /state updates never fetch or overwrite it.
        },
        destroy() {
            commands.forEach((button) => button.removeEventListener('click', onCommand));
            budget?.removeEventListener('click', onBudget);
            disposeCopilot();
            disposePlacement();
            consciousness?.destroy();
        },
    };
}
