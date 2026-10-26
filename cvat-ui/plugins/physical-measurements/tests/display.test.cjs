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
    const factory = vm.runInThisContext(`(function(module, exports, require, window) {${result.outputText}\n})`, { filename: file });
    factory(module, module.exports, (id) => {
        if (id in mocks) return mocks[id];
        throw Error(`Unexpected import: ${id}`);
    }, globals.window);
    return module.exports;
}
const measurement = load('measurement.ts');
const displayModule = load('display.ts', { './measurement': measurement });
const { createDisplay, shapeText } = displayModule;
const tag = (value = '0.4\\0.3', frame = 100) => ({
    objectType: 'tag', frame, label: { name: 'DicomTag', attributes: [{ id: 17, name: '00280030' }] },
    attributes: { 17: value },
});
const shape = (frame = 100) => ({ frame, shapeType: 'polyline', points: [0, 0, 10, 0, 10, 10] });
const deferred = () => {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('the formatter measures only the current frame and polyline geometry', () => {
    const text = shapeText(100, { x: 0.3, y: 0.4, unit: 'mm' });
    assert.equal(text(shape()), '7 mm');
    assert.equal(text({ ...shape(), points: [0, 0, 10, 10] }), '5 mm');
    assert.equal(text(shape(101)), null);
    assert.equal(text({ ...shape(), shapeType: 'polygon' }), null);
    assert.equal(shapeText(100, null)(shape()), '20 px');
    assert.equal(shapeText(-1, null)(shape(-1)), null);
});

test('display reads the unfiltered in-memory collection and follows unsaved edits, deletion and undo', async () => {
    let tags = [tag()]; const reads = []; const calls = [];
    const target = { key: 'job', frame: 100, revision: [],
        canvas: { configure: ({ shapeText: text }) => calls.push(text) },
        annotations: { get: async (...args) => { reads.push(args); return tags; } } };
    const display = createDisplay(assert.fail);
    display.update(target); await tick();
    assert.deepEqual(reads, [[100, false, []]]);
    assert.equal(calls.at(-1)(shape()), '7 mm');
    for (const [next, expected] of [[[tag('0.8\\0.6')], '14 mm'], [[], '20 px'], [[tag()], '7 mm']]) {
        tags = next;
        display.update({ ...target, revision: [] });
        assert.equal(calls.at(-1), null);
        await tick(); assert.equal(calls.at(-1)(shape()), expected);
    }
    assert.ok(reads.every((args) => args[1] === false && args[2].length === 0));
});

test('a later frame response wins and earlier metadata can never label the current frame', async () => {
    const first = deferred(); const second = deferred(); const calls = [];
    const target = { key: 'job', frame: 100, revision: [],
        canvas: { configure: ({ shapeText: text }) => calls.push(text) },
        annotations: { get: (frame) => (frame === 100 ? first.promise : second.promise) } };
    const display = createDisplay(assert.fail);
    display.update(target);
    display.update({ ...target, frame: 101, revision: [] });
    assert.equal(calls.at(-1), null);
    second.resolve([tag('0.8\\0.6', 101)]); await tick();
    const latest = calls.at(-1);
    assert.equal(latest(shape(101)), '14 mm');
    assert.equal(latest(shape()), null);
    first.resolve([tag()]); await tick();
    assert.equal(calls.at(-1), latest);
});

test('changing the job or identity clears its text and rejects a stale response', async () => {
    for (const key of ['other-job', 'other-user', 'other-organization']) {
        const pending = deferred(); const calls = [];
        const target = { key: 'original', frame: 100, revision: [],
            canvas: { configure: ({ shapeText: text }) => calls.push(text) },
            annotations: { get: () => pending.promise } };
        const display = createDisplay(assert.fail);
        display.update(target);
        display.update({ ...target, key, annotations: { get: async () => [] } });
        await tick();
        const latest = calls.at(-1);
        assert.equal(latest(shape()), '20 px');
        pending.resolve([tag()]); await tick();
        assert.equal(calls.at(-1), latest);
        display.dispose(); assert.equal(calls.at(-1), null);
    }
});

test('unrelated Redux notifications neither reread annotations nor redraw labels', async () => {
    let reads = 0; const calls = [];
    const target = { key: 'job', frame: 100, revision: [],
        canvas: { configure: ({ shapeText: text }) => calls.push(text) },
        annotations: { get: async () => { reads++; return [tag()]; } } };
    const display = createDisplay(assert.fail);
    display.update(target); display.update({ ...target }); await tick();
    const count = calls.length;
    display.update({ ...target }); await tick();
    assert.equal(reads, 1); assert.equal(calls.length, count);
});

test('read errors clear previous measurements and disposal cancels late redraws and error reports', async () => {
    const calls = []; const errors = []; let pending = deferred();
    const target = { key: 'job', frame: 100, revision: [],
        canvas: { configure: ({ shapeText: text }) => calls.push(text) },
        annotations: { get: () => pending.promise } };
    const display = createDisplay((error) => errors.push(error));
    display.update(target); pending.resolve([tag()]); await tick();
    assert.equal(calls.at(-1)(shape()), '7 mm');
    pending = deferred(); display.update({ ...target, revision: [] });
    const failure = new Error('unavailable'); pending.reject(failure); await tick();
    assert.equal(calls.at(-1), null); assert.deepEqual(errors, [failure]);
    pending = deferred(); display.update({ ...target, revision: [] }); display.dispose();
    const count = calls.length; pending.resolve([tag()]); await tick();
    assert.equal(calls.length, count);
});

test('plugin waits for frame readiness, ignores filtered states and removes no metadata on shutdown', async () => {
    let builder; let ready; let registrations = 0; let reads = 0; let tags = [tag()];
    const calls = [];
    const annotations = { get: async () => { reads++; return tags; } };
    const state = { auth: { user: { id: 1 } }, organizations: { current: null },
        annotation: { job: { instance: { id: 8, dimension: '2d', annotations }, fetching: false },
            annotations: { states: [], initialized: true, filters: [{ '==': [{ var: 'label' }, 'Length'] }] },
            canvas: { instance: { configure: ({ shapeText: text }) => calls.push(text) }, ready: false },
            player: { frame: { number: 100, fetching: false } } } };
    load('index.tsx', { './display': displayModule }, {
        window: { cvatUI: { registerComponent: (value) => { builder = value; registrations++; } },
            addEventListener: (name, listener) => { assert.equal(name, 'plugins.ready'); ready = listener; } },
    });
    ready(); assert.equal(registrations, 1);
    const plugin = builder({ store: { getState: () => state } });
    assert.equal(reads, 0);
    state.annotation.canvas.ready = true; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1)(shape()), '7 mm');
    plugin.globalStateDidUpdate(); await tick(); assert.equal(reads, 1);
    tags = []; state.annotation.annotations.states = []; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1)(shape()), '20 px');
    tags = [tag()]; state.annotation.annotations.states = []; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1)(shape()), '7 mm');
    state.annotation.player.frame.fetching = true; plugin.globalStateDidUpdate();
    assert.equal(calls.at(-1), null);
    state.annotation.player.frame.fetching = false; state.auth.user = null; plugin.globalStateDidUpdate();
    const count = reads; plugin.destructor(); plugin.globalStateDidUpdate(); await tick();
    assert.equal(reads, count); assert.deepEqual(tags, [tag()]);
});

test('identity changes cannot reuse another identity\'s cached Job, while returning to its owner is allowed', async () => {
    let builder; let reads = 0; const calls = [];
    const job = { id: 8, dimension: '2d', annotations: {
        get: async () => { reads++; return [tag()]; },
    } };
    const state = { auth: { user: { id: 1 } }, organizations: { current: null },
        annotation: { job: { instance: job, fetching: false },
            annotations: { states: [], initialized: true },
            canvas: { instance: { configure: ({ shapeText: text }) => calls.push(text) }, ready: true },
            player: { frame: { number: 100, fetching: false } } } };
    load('index.tsx', { './display': displayModule }, {
        window: { cvatUI: { registerComponent: (value) => { builder = value; } }, addEventListener() {} },
    });
    const plugin = builder({ store: { getState: () => state } });
    await tick(); assert.equal(calls.at(-1)(shape()), '7 mm');
    state.organizations.current = { id: 2, slug: 'other' }; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1), null); assert.equal(reads, 1);
    state.organizations.current = null; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1)(shape()), '7 mm'); assert.equal(reads, 2);
    state.auth.user = { id: 2 }; plugin.globalStateDidUpdate(); await tick();
    assert.equal(calls.at(-1), null); assert.equal(reads, 2);
    state.annotation.job.instance = { ...job, annotations: { get: async () => [] } };
    plugin.globalStateDidUpdate(); await tick(); assert.equal(calls.at(-1)(shape()), '20 px');
    plugin.destructor();
});
