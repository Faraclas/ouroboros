import assert from 'node:assert/strict';
import test from 'node:test';

import { renderSettingsPage } from '../modules/settings_ui.js';

test('standard pricing is an unchecked, labeled checkbox within Behavior / Context Mode', () => {
    const html = renderSettingsPage();
    const behavior = html.slice(html.indexOf('data-settings-panel="behavior"'));
    const context = behavior.slice(behavior.indexOf('<h3>Context Mode</h3>'), behavior.indexOf('<h3>Image Input</h3>'));
    assert.equal(html.match(/id="s-copilot-prefer-standard-tier"/g)?.length, 1);
    const checkbox = context.match(/<input type="checkbox" id="s-copilot-prefer-standard-tier"[^>]*>/)?.[0];
    assert.ok(checkbox);
    assert.match(checkbox, /aria-describedby="s-copilot-prefer-standard-tier-help"/);
    assert.doesNotMatch(checkbox, /\bchecked\b/);
    assert.match(context, /Prefer Copilot standard pricing in Max/);
    assert.match(context, /soft target, not a spending limit/);
    assert.match(context, /context is already above the tier, tool output uses the full window instead/);
    assert.match(context, /Low and Nano are unchanged/);
});
