import test from 'node:test';
import assert from 'node:assert/strict';
import { formatNativePath } from '../modules/ui_primitives.js';

test('native path display is reversible and never aliases escaped-looking names', () => {
    const names = ['raw_\udcff', 'raw_\udcfe', 'raw_ÿ', String.raw`raw_\udcff`, 'raw_�', 'a\nb', ' trailing ', 'emoji_🐍', 'bidi_\u202e', 'invisible_\u200b'];
    const shown = names.map(formatNativePath);
    assert.equal(new Set(shown).size, names.length);
    for (let i = 0; i < names.length; i++) {
        assert.equal(shown[i].startsWith('"') ? JSON.parse(shown[i]) : shown[i], names[i]);
        assert.ok(!/[\ud800-\udfff]/u.test(shown[i]));
    }
    assert.equal(formatNativePath('/ordinary/日本語/🐍'), '/ordinary/日本語/🐍');
});
