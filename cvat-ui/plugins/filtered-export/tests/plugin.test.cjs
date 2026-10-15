// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const source = path.resolve(__dirname, '../src/ts');
function load(name, mocks = {}, globals = {}) {
    const file = path.join(source, name);
    const result = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.React, esModuleInterop: true },
    });
    assert.deepEqual((result.diagnostics || []).filter((d) => d.category === ts.DiagnosticCategory.Error), []);
    const module = { exports: {} };
    vm.runInNewContext(result.outputText, {
        module, exports: module.exports, AbortController, console,
        require: (id) => { if (id in mocks) return mocks[id]; throw Error(`Unexpected import: ${id}`); },
        ...globals,
    }, { filename: file });
    return module.exports;
}
const { createAPI, errorMessage } = load('api.ts');
const selection = { filter: { mode: 'any', tag_ids: [1], start: 0, stop: null, step: 1 },
    format: 'CVAT for images 1.1', saveImages: true, omitTags: false };

test('uses the configured client, explicit organization and local storage', async () => {
    const requests = [];
    const api = createAPI(async (request) => { requests.push(request); return { data: {} }; },
        '/api/', 'task', 7, 'team', () => 'owner');
    await api.options();
    await api.preview(selection);
    await api.export(selection, 'revision');
    assert.equal(requests[0].url, '/api/filtered-export/tasks/7/options');
    assert.equal(requests[0].params.org, 'team');
    assert.equal(requests[1].method, 'POST');
    assert.equal(requests[1].params.location, 'local');
    assert.equal(requests[1].params.use_default_location, false);
    assert.equal(requests[1].params.save_images, true);
    assert.equal(requests[1].data.expected_revision, undefined);
    assert.equal(requests[2].data.expected_revision, 'revision');
});

test('project path is separate and IDs are validated', async () => {
    let request;
    const api = createAPI(async (r) => { request = r; return { data: {} }; }, '/api', 'project', 2, '', () => 'u');
    await api.preview(selection);
    assert.equal(request.url, '/api/filtered-export/projects/2/preview');
    for (const id of [0, -1, 1.5, NaN]) assert.throws(() => createAPI(() => {}, '/api', 'task', id, '', () => 'u'));
});

test('identity mismatch is permanent, including after switching back', async () => {
    let identity = 'a';
    let calls = 0;
    const api = createAPI(async () => { calls++; return { data: {} }; }, '/api', 'task', 1, '', () => identity);
    identity = 'b';
    await assert.rejects(api.options());
    identity = 'a';
    await assert.rejects(api.options());
    assert.equal(calls, 0);
});

test('in-flight responses are rejected after identity change', async () => {
    let identity = 'a';
    let finish;
    const api = createAPI(() => new Promise((resolve) => { finish = resolve; }), '/api', 'task', 1, '', () => identity);
    const pending = api.preview(selection);
    identity = 'b';
    finish({ data: { count: 2 } });
    await assert.rejects(pending);
});

test('dispose aborts the request and prevents further requests', async () => {
    let request;
    const api = createAPI(async (r) => { request = r; return { data: {} }; }, '/api', 'task', 1, '', () => 'u');
    await api.options();
    api.dispose();
    assert.equal(request.signal.aborted, true);
    await assert.rejects(api.preview(selection));
});

test('409 attaches to existing export, without retrying POST', async () => {
    let calls = 0;
    const api = createAPI(async () => { calls++; throw { response: { status: 409, data: { rq_id: 'existing' } } }; },
        '/api', 'task', 1, '', () => 'u');
    assert.equal((await api.export(selection, 'revision')).rq_id, 'existing');
    assert.equal(calls, 1);
    await assert.rejects(api.preview(selection));
});

test('network errors and malformed conflicts are not retried', async () => {
    for (const reason of [new Error('timeout'), { response: { status: 409, data: {} } }]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; throw reason; }, '/api', 'task', 1, '', () => 'u');
        await assert.rejects(api.export(selection, 'revision'));
        assert.equal(calls, 1);
    }
});

test('validation errors are readable', () => {
    assert.equal(errorMessage({ response: { data: ['invalid tag', 'invalid range'] } }), 'invalid tag\ninvalid range');
    assert.equal(errorMessage({ response: { data: 'failure' } }), 'failure');
    assert.equal(errorMessage(null), 'null');
});

test('plugin registers once and produces native menu objects, not React elements', () => {
    let builder;
    let ready;
    let registrations = 0;
    let opens = 0;
    let focuses = 0;
    let disposals = 0;
    const dispatched = [];
    const state = { auth: { user: { id: 1 } }, organizations: { current: { id: 1, slug: 'team' } },
        tasks: { selected: [] }, projects: { selected: [] } };
    load('index.tsx', {
        '@modules/axios': { __esModule: true, default: { request: () => {} } },
        './api': { createAPI: () => ({}) },
        './dialog': { openDialog: () => { opens++; return { focus: () => { focuses++; }, dispose: () => { disposals++; } }; } },
    }, { window: {
        cvatUI: { registerComponent: (b) => { builder = b; registrations++; } },
        addEventListener: (name, listener) => { assert.equal(name, 'plugins.ready'); ready = listener; },
    } });
    ready();
    assert.equal(registrations, 1);
    const plugin = builder({ core: { config: { backendAPI: '/api' } }, store: { getState: () => state },
        dispatch: (action) => dispatched.push(action), actionCreators: {
            addUIComponent: (path, component) => ({ kind: 'add', path, component }),
            removeUIComponent: (path, component) => ({ kind: 'remove', path, component }),
        } });
    assert.deepEqual(dispatched.map((x) => x.path), ['projectActions.items', 'taskActions.items']);
    const component = dispatched[1].component;
    const props = { targetProps: { taskInstance: { id: 7, name: 'task', dimension: '2d' } } };
    const item = component(props);
    assert.equal(item.disabled, false);
    assert.equal(item.key, 'filtered-export');
    assert.equal(typeof item.onClick, 'function');
    item.onClick(); item.onClick();
    assert.equal(opens, 1); assert.equal(focuses, 1);
    state.tasks.selected = [1, 2];
    assert.equal(component(props).disabled, true);
    state.tasks.selected = [];
    props.targetProps.taskInstance.dimension = '3d';
    assert.equal(component(props).disabled, true);
    state.organizations.current.id = 2;
    plugin.globalStateDidUpdate();
    assert.equal(disposals, 1);
    plugin.destructor();
    assert.equal(dispatched.filter((x) => x.kind === 'remove').length, 2);
});

test('dialog TSX has no transpilation syntax errors', () => {
    const file = path.join(source, 'dialog.tsx');
    const result = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
    });
    assert.deepEqual((result.diagnostics || []).filter((d) => d.category === ts.DiagnosticCategory.Error), []);
});
