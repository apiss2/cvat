// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

const source = path.resolve(__dirname, '../src/ts');
function load(name, mocks = {}, globals = {}) {
    const file = path.join(source, name);
    const result = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.React, esModuleInterop: true },
    });
    assert.deepEqual(result.diagnostics, []);
    const module = { exports: {} };
    const factory = vm.runInThisContext(`(function(module, exports, require, window, document) {${result.outputText}\n})`, { filename: file });
    factory(module, module.exports, (id) => {
        if (id in mocks) return mocks[id];
        throw Error(`Unexpected import: ${id}`);
    }, globals.window, globals.document);
    return module.exports;
}
const measurement = load('measurement.ts');
const { createAPI, createEditorAPI, errorMessage } = load('api.ts', { './measurement': measurement });
const { createDisplay, shapeText } = load('display.ts', { './measurement': measurement });
const config = { version: 1, default: { x: 0.3, y: 0.4, unit: 'mm' }, frames: { 1: null } };
const document = (extra = {}) => ({ ...structuredClone(config), revision: 3, supported: true,
    unavailable_reason: null, frame_count: 20, ...extra });
const deferred = () => {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('API uses explicit organization, portable config and compare-and-swap revision', async () => {
    const requests = [];
    const api = createAPI(async (request) => { requests.push(request); return { data: document() }; },
        '/api/', 'task', 7, 'team', () => 'user:team');
    await api.get(); await api.save(config, 3);
    assert.equal(requests[0].url, '/api/physical-measurements/tasks/7');
    assert.equal(requests[0].method, 'GET');
    assert.deepEqual(requests[0].params, { org: 'team' });
    assert.equal(requests[1].method, 'PUT');
    assert.deepEqual(requests[1].data, { ...config, expected_revision: 3 });
    assert.equal(requests[0].signal.aborted, false);
    api.dispose(); assert.equal(requests[0].signal.aborted, true);
    await assert.rejects(api.get());
});

test('untrusted calibration responses and out-of-task overrides are rejected', async () => {
    for (const value of [null, {}, document({ revision: -1 }), document({ supported: 'true' }),
        document({ frame_count: 0.5 }), document({ frames: { 20: config.default } }),
        document({ default: { x: 0, y: 1, unit: 'mm' } })]) {
        const api = createAPI(async () => ({ data: value }), '/api', 'job', 1, '', () => 'u');
        await assert.rejects(api.get());
    }
    for (const id of [0, -1, NaN, 0.5]) assert.throws(() => createAPI(() => {}, '/api', 'task', id, '', () => 'u'));
});

test('identity changes fence both future requests and already-running responses', async () => {
    let owner = 'a'; const result = deferred(); let calls = 0;
    const api = createAPI(() => { calls++; return result.promise; }, '/api', 'task', 1, '', () => owner);
    const pending = api.get(); owner = 'b'; result.resolve({ data: document() });
    await assert.rejects(pending);
    owner = 'a'; await assert.rejects(api.get()); assert.equal(calls, 1);
});

test('a revision conflict or lost response is never automatically retried', async () => {
    for (const failure of [{ response: { status: 409 } }, new Error('timeout')]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; throw failure; }, '/api', 'task', 1, '', () => 'u');
        await assert.rejects(api.save(config, 3));
        assert.equal(calls, 1);
    }
    assert.match(errorMessage({ response: { status: 409 } }), /再読込/);
});

test('job calibration is read-only and the editor only falls back on task permission denial', async () => {
    let writes = 0; let disposed = 0;
    const job = createAPI(async () => ({ data: document() }), '/api', 'job', 9, '', () => 'u');
    assert.equal((await job.get()).readOnly, true);
    assert.throws(() => job.save(config, 3));
    const task = { get: async () => { throw { response: { status: 403 } }; },
        save: async () => { writes++; return document(); }, dispose: () => { disposed++; } };
    const editor = createEditorAPI(task, job);
    assert.equal((await editor.get()).readOnly, true);
    assert.throws(() => editor.save(config, 3)); assert.equal(writes, 0);
    editor.dispose(); assert.equal(disposed, 1);
    for (const status of [404, 500]) {
        let fallback = 0;
        const broken = createEditorAPI({ ...task, get: async () => { throw { response: { status } }; } },
            { ...job, get: async () => { fallback++; return document(); } });
        await assert.rejects(broken.get()); assert.equal(fallback, 0);
    }
});

test('a frame-aware label handles anisotropy, explicit exclusions, editing and ordinary pixels', () => {
    const text = shapeText(document());
    const shape = { frame: 0, shapeType: 'polyline', points: [0, 0, 10, 0, 10, 10] };
    assert.equal(text(shape), '7 mm');
    assert.equal(text({ ...shape, frame: 1 }), '20 px');
    assert.equal(text({ ...shape, frame: 2 }), '7 mm');
    assert.equal(text({ ...shape, points: [0, 0, 10, 10] }), '5 mm');
    assert.equal(text({ ...shape, shapeType: 'polygon' }), null);
    assert.equal(text({ ...shape, frame: 20 }), null);
    assert.equal(shapeText(document({ default: null, frames: {} }))(shape), '20 px');
    assert.equal(shapeText(document({ supported: false }))(shape), null);
});

test('job switches clear labels immediately and reject an older response arriving last', async () => {
    const first = deferred(); const second = deferred(); const calls = []; const errors = [];
    const canvas = { configure: (options) => calls.push(options.shapeText) };
    let disposed = 0;
    const display = createDisplay((error) => errors.push(error));
    display.update({ key: 'job1', canvas, api: { get: () => first.promise, dispose: () => { disposed++; } } });
    display.update({ key: 'job2', canvas, api: { get: () => second.promise, dispose: () => { disposed++; } } });
    assert.equal(calls.at(-1), null);
    second.resolve(document({ default: { x: 1, y: 1, unit: 'mm' } })); await tick();
    const latest = calls.at(-1);
    assert.equal(latest({ frame: 0, shapeType: 'polyline', points: [0, 0, 3, 4] }), '5 mm');
    first.resolve(document()); await tick(); assert.equal(calls.at(-1), latest);
    display.dispose(); assert.equal(calls.at(-1), null); assert.equal(disposed, 2);
    assert.deepEqual(errors, []);
});

test('reload errors remove calibration and disposal prevents asynchronous redraws', async () => {
    const calls = []; const errors = []; let pending;
    const display = createDisplay((error) => errors.push(error));
    const api = { get: () => pending.promise, dispose: () => {} };
    const target = { key: 'job1', canvas: { configure: (options) => calls.push(options.shapeText) }, api };
    pending = deferred(); display.update(target);
    pending.resolve(document()); await tick(); assert.equal(typeof calls.at(-1), 'function');
    pending = deferred(); const refresh = display.refresh(); assert.equal(calls.at(-1), null);
    pending.reject(new Error('network')); await refresh;
    assert.equal(errors.length, 1); assert.equal(calls.at(-1), null);
    pending = deferred(); const next = display.refresh(); display.dispose();
    const count = calls.length; pending.resolve(document()); await next;
    assert.equal(calls.length, count);
});

test('repeated Redux notifications do not reload the same job or reset its canvas', async () => {
    let calls = 0; let loads = 0;
    const display = createDisplay(() => {});
    const target = { key: 'job', canvas: { configure: () => { calls++; } },
        api: { get: async () => { loads++; return document(); }, dispose: () => {} } };
    display.update(target); await tick(); const count = calls;
    display.update(target); display.update(target); await tick();
    assert.equal(loads, 1); assert.equal(calls, count); display.dispose();
});

test('plugin registers optional menus once, waits for canvas readiness and clears on logout', () => {
    let builder; let ready; let registrations = 0; let refreshes = 0; let disposals = 0;
    const updates = []; const actions = []; const listeners = new Map();
    const state = { auth: { user: { id: 1 } }, organizations: { current: null }, tasks: { selected: [] },
        annotation: { job: { instance: { id: 8, taskId: 4, dimension: '2d' }, fetching: false },
            canvas: { instance: { configure: () => {} }, ready: false }, player: { frame: { number: 3 } } } };
    load('index.tsx', {
        '@modules/axios': { __esModule: true, default: { request: () => {} } },
        '@modules/antd/lib/notification': { __esModule: true, default: { warning: () => {} } },
        './api': { createAPI: () => ({}), createEditorAPI: () => ({}), errorMessage: String },
        './display': { createDisplay: () => ({ update: (next) => updates.push(next),
            refresh: async () => { refreshes++; }, dispose: () => { disposals++; } }) },
        './dialog': { openDialog: () => ({ dispose: () => {}, focus: () => {} }) },
    }, { window: { cvatUI: { registerComponent: (value) => { builder = value; registrations++; } },
        addEventListener: (name, value) => { if (name === 'plugins.ready') ready = value; listeners.set(name, value); },
        removeEventListener: (name) => listeners.delete(name) }, document: { hidden: false } });
    ready(); assert.equal(registrations, 1);
    const plugin = builder({ core: { config: { backendAPI: '/api' } }, store: { getState: () => state },
        dispatch: (action) => actions.push(action), actionCreators: {
            addUIComponent: (path, component) => ({ type: 'add', path, component }),
            removeUIComponent: (path, component) => ({ type: 'remove', path, component }),
        } });
    assert.deepEqual(actions.map((action) => action.path), ['taskActions.items', 'annotationPage.menuActions.items']);
    assert.equal(updates.at(-1), null);
    state.annotation.canvas.ready = true; plugin.globalStateDidUpdate();
    assert.match(updates.at(-1).key, /:8$/); const count = updates.length;
    state.annotation.canvas.ready = false; state.annotation.player.frame.number = 4;
    plugin.globalStateDidUpdate(); assert.equal(updates.length, count);
    listeners.get('focus')(); assert.equal(refreshes, 1);
    state.auth.user = null; plugin.globalStateDidUpdate(); assert.equal(updates.at(-1), null);
    plugin.destructor(); assert.equal(disposals, 1); assert.equal(listeners.has('focus'), false);
    assert.equal(actions.filter((action) => action.type === 'remove').length, 2);
});
