import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../modules/room_activity.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '').replace(/export /g, '');
function factory() {
    const context = vm.createContext({ AbortController, tr: (_key, english) => english,
        fmt: (text, args) => text.replace(/\{(\w+)\}/g, (_whole, key) => args[key]) });
    vm.runInContext(source + '\nthis.create = createActivityHistory;', context);
    return context.create;
}
const tick = () => new Promise(resolve => setImmediate(resolve));
test('retained older rows retain earlier omission notices until their membership view changes', async () => {
    const pages = [
        { view: 'one', tasks: [{ task_id: 'new' }], gaps: ['A record could not be read.'], next_cursor: 'older' },
        { view: 'one', tasks: [{ task_id: 'old' }], gaps: [], next_cursor: null, coverage: { reached_start: true } },
        { view: 'one', tasks: [{ task_id: 'new' }], gaps: [], next_cursor: 'older' },
        { view: 'two', tasks: [{ task_id: 'different' }], gaps: [], next_cursor: null, coverage: { reached_start: true } },
    ];
    const reader = factory()({ chatId: 1, changed() {}, fetchPage: async () => pages.shift() });
    reader.open(); await tick();
    assert.match(reader.state().message, /could not be read/);
    await reader.older();
    assert.equal(reader.rows().length, 2);
    assert.match(reader.state().message, /could not be read/);
    await reader.refresh();
    assert.match(reader.state().message, /could not be read/);
    await reader.refresh();
    assert.equal(reader.rows().length, 1);
    assert.equal(reader.state().message, '');
    reader.destroy();
});
