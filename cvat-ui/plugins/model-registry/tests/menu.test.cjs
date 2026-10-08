// SPDX-License-Identifier: MIT
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

test('CVAT menu opens the registry and is removed when the plugin unloads', async () => {
    const events = [];
    let plugin;
    global.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
    global.window = {
        cvatUI: { registerComponent(builder) {
            plugin = builder({
                core: { lambda: { list: async () => ({ models: [] }) } },
                store: { getState: () => ({ auth: { user: { id: 1 } } }) },
                dispatch: (action) => events.push(action),
                actionCreators: {
                    getModelsSuccess: () => ({ type: 'models' }),
                    addUIComponent: (location, factory, data) => ({ type: 'add', location, factory, data }),
                    removeUIComponent: (location, factory) => ({ type: 'remove', location, factory }),
                },
            });
        } },
        setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {},
        open: (...args) => events.push({ type: 'open', args }),
    };
    require(path.join(process.env.MODEL_REGISTRY_TEST_BUILD, 'index.js'));
    const add = events.find((event) => event.type === 'add');
    assert.equal(add.location, 'header.userMenu.items');
    const item = add.factory(); assert.equal(item.label, 'モデル管理'); item.onClick();
    assert.deepEqual(events.find((event) => event.type === 'open').args, ['/model-registry/#models', '_blank', 'noopener,noreferrer']);
    plugin.destructor();
    const remove = events.find((event) => event.type === 'remove');
    assert.equal(remove.location, add.location); assert.equal(remove.factory, add.factory);
});
