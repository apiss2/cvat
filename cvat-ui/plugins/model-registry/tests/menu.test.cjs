// SPDX-License-Identifier: MIT
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function setup(t, initialModels = []) {
    const events = [];
    let plugin;
    let poll;
    let models = initialModels;
    const modulePath = path.join(process.env.MODEL_REGISTRY_TEST_BUILD, 'index.js');
    const previousWindow = global.window;
    const previousDocument = global.document;
    global.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
    global.window = {
        cvatUI: { registerComponent(builder) {
            plugin = builder({
                core: { lambda: { list: async () => ({ models }) } },
                store: { getState: () => ({ auth: { user: { id: 1 } } }) },
                dispatch: (action) => events.push(action),
                actionCreators: {
                    getModelsSuccess: (rows, count) => ({ type: 'models', models: rows, count }),
                    addUIComponent: (location, factory, data) => ({ type: 'add', location, factory, data }),
                    removeUIComponent: (location, factory) => ({ type: 'remove', location, factory }),
                },
            });
        } },
        setInterval: (callback) => { poll = callback; return 1; },
        clearInterval() {}, addEventListener() {}, removeEventListener() {},
        open: (...args) => events.push({ type: 'open', args }),
    };
    delete require.cache[require.resolve(modulePath)];
    require(modulePath);
    let disposed = false;
    const dispose = () => {
        if (!disposed) { disposed = true; plugin.destructor(); }
    };
    t.after(() => {
        dispose();
        delete require.cache[require.resolve(modulePath)];
        if (previousWindow === undefined) delete global.window;
        else global.window = previousWindow;
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
    });
    return {
        events,
        setModels: (next) => { models = next; },
        poll: async () => { poll(); await new Promise(setImmediate); },
        dispose,
        updates: () => events.filter((event) => event.type === 'models'),
    };
}

test('CVAT menu opens the registry and is removed when the plugin unloads', async (t) => {
    const { events, dispose } = setup(t);
    const add = events.find((event) => event.type === 'add');
    assert.equal(add.location, 'header.userMenu.items');
    const item = add.factory(); assert.equal(item.label, 'モデル管理'); item.onClick();
    assert.deepEqual(events.find((event) => event.type === 'open').args, ['/model-registry/#models', '_blank', 'noopener,noreferrer']);
    dispose();
    const remove = events.find((event) => event.type === 'remove');
    assert.equal(remove.location, add.location); assert.equal(remove.factory, add.factory);
});

test('revision changes, rollback and deletion refresh models without the removed version property', async (t) => {
    // The removed getter must not silently become an undefined fingerprint field.
    const model = (revision) => new Proxy({ id: `mr-model-${revision}`, name: 'Detector' }, {
        get(target, property, receiver) {
            assert.notEqual(property, 'version', 'MLModel.version is no longer public');
            return Reflect.get(target, property, receiver);
        },
    });
    const first = model('revision-one');
    const s = setup(t, [first]);
    await new Promise(setImmediate);
    assert.equal(s.updates().length, 1);
    assert.equal(s.updates()[0].models[0], first);

    s.setModels([model('revision-one')]);
    await s.poll();
    assert.equal(s.updates().length, 1, 'unchanged results preserve existing model instances');

    const second = model('revision-two');
    s.setModels([second]);
    await s.poll();
    assert.equal(s.updates().length, 2);
    assert.equal(s.updates().at(-1).models[0], second);

    s.setModels([first]);
    await s.poll();
    assert.equal(s.updates().length, 3);
    assert.equal(s.updates().at(-1).models[0], first);

    s.setModels([]);
    await s.poll();
    assert.deepEqual(s.updates().at(-1), { type: 'models', models: [], count: 0 });
});

test('catalog ordering does not refresh models but a name change does', async (t) => {
    const a = { id: 'a', name: 'First' };
    const b = { id: 'b', name: 'Second' };
    const s = setup(t, [a, b]);
    await new Promise(setImmediate);
    s.setModels([b, a]);
    await s.poll();
    assert.equal(s.updates().length, 1);
    const renamed = { ...a, name: 'Renamed' };
    s.setModels([b, renamed]);
    await s.poll();
    assert.equal(s.updates().length, 2);
    assert.equal(s.updates().at(-1).models[1], renamed);
});
