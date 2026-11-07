import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const app = fs.readFileSync(new URL('../app.js', import.meta.url), 'utf8');
const body = app.slice(app.indexOf('function handoverBetween('), app.indexOf('\nfunction syncNavigationState('));
test('project handover animates the complete owned room, including its header', () => {
    const oldHost = { name: 'old room' }, newHost = { name: 'new room' };
    const oldPage = { closest: selector => selector === '.viper-room-pane' ? oldHost : null };
    const newPage = { closest: selector => selector === '.viper-room-pane' ? newHost : null };
    const panel = { children: [] }, main = {};
    const context = vm.createContext({ rooms: { retiring: new Map([['old', { page: oldPage }]]) },
        projectInstances: new Map([['new', { page: newPage }]]), projectPanel: panel,
        document: { getElementById: () => main } });
    vm.runInContext(body + '\nthis.handover = handoverBetween;', context);
    const both = context.handover({ project: 'old' }, { project: 'new' });
    assert.equal(both.leaving[0], oldHost);
    assert.equal(both.arriving, newHost);
    assert.equal(both.breathe[0], newHost);
    const toMain = context.handover({ project: 'new' }, { page: 'chat' });
    assert.equal(toMain.leaving[0], panel);
    assert.equal(toMain.arriving, main);
});
