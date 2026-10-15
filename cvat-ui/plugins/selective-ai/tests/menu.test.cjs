// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const load = require('./load.cjs');

function fixture() {
    const actions = []; const dialogs = []; const events = {}; let plugin; let focus = 0; let disposed = 0;
    const state = { auth: { user: { id: 10 } }, organizations: { current: { id: 7, slug: 'team' } },
        tasks: { selected: [] }, projects: { selected: [] } };
    const args = {
        core: { config: { backendAPI: '/api' }, lambda: { list: async () => ({ models: [] }) } },
        store: { getState: () => state }, dispatch: (action) => { actions.push(action); },
        actionCreators: {
            addUIComponent: (path, component, data) => ({ type: 'add', path, component, data }),
            removeUIComponent: (path, component) => ({ type: 'remove', path, component }),
        },
    };
    const previous = global.window;
    global.window = { addEventListener: (name, handler) => { events[name] = handler; },
        cvatUI: { registerComponent: (builder) => { plugin = builder(args); } } };
    try {
        load('index.tsx', {
            '@modules/axios': { default: { request: async () => ({ data: {} }) } },
            './api': { createAPI: (...args) => ({ args }) },
            './dialog': { openDialog: (options) => {
                dialogs.push(options); return { focus: () => { focus++; }, dispose: () => { disposed++; options.onClose(); } };
            } },
        });
        events['plugins.ready']();
    } finally { global.window = previous; }
    return { actions, dialogs, state, plugin, get focus() { return focus; }, get disposed() { return disposed; } };
}

test('registers two menu factories once without replacing upstream menu components', () => {
    const f = fixture(); assert.deepEqual(f.actions.map((a) => a.path), ['projectActions.items', 'taskActions.items']);
    const item = f.actions[1].component({ targetProps: { taskInstance: { id: 5, name: 'task', dimension: '2d' } } });
    assert.equal(item.key, 'selective-ai'); assert.equal(item.disabled, false); assert.equal(typeof item.onClick, 'function');
    item.onClick(); assert.deepEqual(f.dialogs[0].scope, { type: 'task', id: 5 });
    assert.equal(f.dialogs[0].api.args[1], '/api'); assert.equal(f.dialogs[0].api.args[2], 'team');
});

test('project and task actions resolve targetProps and a second invocation reuses the open dialog', () => {
    const f = fixture(); const item = f.actions[0].component({ targetProps: { projectInstance: { id: 7, name: 'project', dimension: '2d' } } });
    item.onClick(); item.onClick(); assert.equal(f.dialogs.length, 1); assert.equal(f.focus, 1);
    assert.deepEqual(f.dialogs[0].scope, { type: 'project', id: 7 });
});

test('unsupported dimension and multi-selection are disabled rather than changing the scope silently', () => {
    const f = fixture(); f.state.tasks.selected = [1, 2];
    assert.equal(f.actions[1].component({ targetProps: { taskInstance: { id: 5, dimension: '2d' } } }).disabled, true);
    assert.equal(f.actions[0].component({ targetProps: { projectInstance: { id: 7, dimension: '3d' } } }).disabled, true);
});

test('identity change cancels the dialog and destructor removes precisely the added factories', () => {
    const f = fixture(); f.actions[1].component({ targetProps: { taskInstance: { id: 5, dimension: '2d' } } }).onClick();
    f.state.auth.user = null; f.plugin.globalStateDidUpdate(); assert.equal(f.disposed, 1);
    f.plugin.destructor(); assert.equal(f.actions.length, 4);
    for (let i = 0; i < 2; i++) {
        assert.equal(f.actions[i + 2].type, 'remove');
        assert.equal(f.actions[i + 2].path, f.actions[i].path);
        assert.strictEqual(f.actions[i + 2].component, f.actions[i].component);
    }
});
