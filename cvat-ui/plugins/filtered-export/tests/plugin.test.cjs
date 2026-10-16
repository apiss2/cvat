// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const source = path.resolve(__dirname, '../src/ts');
const plain = (value) => JSON.parse(JSON.stringify(value));
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
const { buildFields, completeTree } = load('filter-config.ts');
const filters = [{ and: [{ '==': [{ var: 'type' }, 'tag'] }, { '==': [{ var: 'label' }, 'ready'] }] }];
const selection = { filters, format: 'CVAT for images 1.1', saveImages: true };

test('preview sends only filters, independent of the output format', async () => {
    const requests = [];
    const api = createAPI(async (request) => { requests.push(request); return { data: {} }; },
        '/api/', 'task', 7, 'team', () => 'owner');
    await api.options(); await api.preview(filters); await api.export(selection, 'revision');
    assert.equal(requests[0].url, '/api/filtered-export/tasks/7/options');
    assert.deepEqual(plain(requests[1].data), { filters });
    assert.deepEqual(plain(requests[1].params), { org: 'team', location: 'local' });
    assert.equal(requests[2].params.format, selection.format);
    assert.equal(requests[2].params.location, 'local');
    assert.equal(requests[2].params.save_images, true);
    assert.deepEqual(plain(requests[2].data), { filters, expected_revision: 'revision' });
});

test('ITGformat uses the identical API contract without annotation removal options', async () => {
    let request;
    const api = createAPI(async (r) => { request = r; return { data: {} }; }, '/api', 'project', 2, '', () => 'u');
    await api.export({ ...selection, format: 'ITGformat 1.0' }, 'revision');
    assert.equal(request.url, '/api/filtered-export/projects/2/export');
    assert.deepEqual(plain(request.data), { filters, expected_revision: 'revision' });
    for (const id of [0, -1, 1.5, NaN]) assert.throws(() => createAPI(() => {}, '/api', 'task', id, '', () => 'u'));
});

test('identity mismatch invalidates permanently', async () => {
    let identity = 'a'; let calls = 0;
    const api = createAPI(async () => { calls++; return { data: {} }; }, '/api', 'task', 1, '', () => identity);
    identity = 'b'; await assert.rejects(api.options());
    identity = 'a'; await assert.rejects(api.options());
    assert.equal(calls, 0);
});

test('in-flight responses are rejected after an identity change', async () => {
    let identity = 'a'; let finish;
    const api = createAPI(() => new Promise((resolve) => { finish = resolve; }), '/api', 'task', 1, '', () => identity);
    const pending = api.preview(filters); identity = 'b'; finish({ data: { count: 2 } });
    await assert.rejects(pending);
});

test('dispose aborts and prevents further requests', async () => {
    let request;
    const api = createAPI(async (r) => { request = r; return { data: {} }; }, '/api', 'task', 1, '', () => 'u');
    await api.options(); api.dispose();
    assert.equal(request.signal.aborted, true);
    await assert.rejects(api.preview(filters));
});

test('409 attaches to an existing export without retrying', async () => {
    let calls = 0;
    const api = createAPI(async () => { calls++; throw { response: { status: 409, data: { rq_id: 'existing' } } }; },
        '/api', 'task', 1, '', () => 'u');
    assert.equal((await api.export(selection, 'revision')).rq_id, 'existing');
    assert.equal(calls, 1);
    await assert.rejects(api.preview(filters));
});

test('network errors and malformed conflicts are never retried', async () => {
    for (const reason of [new Error('timeout'), { response: { status: 409, data: {} } }]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; throw reason; }, '/api', 'task', 1, '', () => 'u');
        await assert.rejects(api.export(selection, 'revision'));
        assert.equal(calls, 1);
    }
});

test('validation messages are preserved', () => {
    assert.equal(errorMessage({ response: { data: ['invalid filter', 'unknown field'] } }), 'invalid filter\nunknown field');
    assert.equal(errorMessage({ response: { data: 'failure' } }), 'failure');
});

const labels = [{ id: 1, name: 'ready.v1', type: 'tag', sublabels: [], attributes: [
    { id: 1, name: 'quality.score', input_type: 'number', default_value: '0', values: [] },
    { id: 2, name: 'checked', input_type: 'checkbox', default_value: 'false', values: [] },
    { id: 3, name: 'kind', input_type: 'radio', default_value: 'a', values: ['a', 'b'] },
    { id: 4, name: 'note', input_type: 'text', default_value: '', values: [] },
] }, { id: 2, name: 'person', type: 'skeleton', attributes: [], sublabels: [
    { id: 3, name: 'nose', type: 'points', attributes: [], sublabels: [] },
] }];

test('query fields mirror persisted display fields and escaped attribute paths', () => {
    const fields = buildFields(labels);
    assert.deepEqual(plain(fields.type.fieldSettings.listValues).map((v) => v.value), ['shape', 'track', 'tag']);
    assert.equal(fields.attr.subfields['ready\u2219v1'].subfields['quality\u2219score'].type, 'number');
    assert.equal(fields.attr.subfields['ready\u2219v1'].subfields.checked.type, 'boolean');
    assert.equal(fields.attr.subfields['ready\u2219v1'].subfields.kind.type, 'select');
    assert.equal(fields.attr.subfields['ready\u2219v1'].subfields.note.type, 'text');
    for (const key of ['label', 'shape', 'occluded', 'source', 'width', 'height', 'rotation', 'serverID', 'score', 'zOrder']) {
        assert.ok(fields[key], key);
    }
    for (const key of ['start', 'stop', 'step', 'frame', 'objectID', 'votes']) assert.equal(fields[key], undefined);
});

test('keypoint fields use the same parent / child naming convention', () => {
    const fields = buildFields(labels, true);
    assert.deepEqual(plain(fields.label.fieldSettings.listValues), [{ value: 'person / nose', title: 'person / nose' }]);
    assert.equal(fields.type, undefined);
    assert.ok(fields.occluded);
});

const operators = { equal: {}, select_equals: {}, select_any_in: {}, between: { cardinality: 2 }, is_empty: { cardinality: 0 } };
const leaf = (value) => ({ type: 'rule', properties: { field: 'label', operator: 'equal', value: [value] } });

test('unfinished rules are rejected instead of silently widening selection', () => {
    assert.equal(completeTree({ type: 'group' }, operators), true);
    assert.equal(completeTree({ type: 'group', children1: [leaf('ready'), { type: 'rule' }] }, operators), false);
    assert.equal(completeTree({ type: 'group', children1: [leaf('ready'), { type: 'group' }] }, operators), false);
    assert.equal(completeTree(leaf(null), operators), false);
    assert.equal(completeTree(leaf(undefined), operators), false);
    assert.equal(completeTree(leaf([]), operators), false);
});

test('false zero empty text and zero-cardinality operations are valid', () => {
    for (const value of [false, 0, '', ['ready']]) assert.equal(completeTree(leaf(value), operators), true);
    assert.equal(completeTree({ type: 'rule', properties: { field: 'label', operator: 'is_empty', value: [] } }, operators), true);
    assert.equal(completeTree({ type: 'rule', properties: { field: 'width', operator: 'between', value: [1] } }, operators), false);
});

test('plugin menu registration stays isolated and idempotent', () => {
    let builder; let ready; let registrations = 0; let opens = 0; let focuses = 0; let disposals = 0;
    const dispatched = [];
    const state = { auth: { user: { id: 1 } }, organizations: { current: { id: 1, slug: 'team' } },
        tasks: { selected: [] }, projects: { selected: [] } };
    load('index.tsx', {
        '@modules/axios': { __esModule: true, default: { request: () => {} } },
        './api': { createAPI: () => ({}) },
        './dialog': { openDialog: () => { opens++; return { focus: () => { focuses++; }, dispose: () => { disposals++; } }; } },
    }, { window: {
        cvatUI: { registerComponent: (b) => { builder = b; registrations++; } },
        addEventListener: (name, listener) => { ready = listener; },
    } });
    ready(); assert.equal(registrations, 1);
    const plugin = builder({ core: { config: { backendAPI: '/api' } }, store: { getState: () => state },
        dispatch: (action) => dispatched.push(action), actionCreators: {
            addUIComponent: (path, component) => ({ kind: 'add', path, component }),
            removeUIComponent: (path, component) => ({ kind: 'remove', path, component }),
        } });
    assert.deepEqual(dispatched.map((x) => x.path), ['projectActions.items', 'taskActions.items']);
    const component = dispatched[1].component;
    const props = { targetProps: { taskInstance: { id: 7, name: 'task', dimension: '2d' } } };
    const item = component(props); assert.equal(item.disabled, false);
    item.onClick(); item.onClick(); assert.equal(opens, 1); assert.equal(focuses, 1);
    state.tasks.selected = [1, 2]; assert.equal(component(props).disabled, true);
    state.organizations.current.id = 2; plugin.globalStateDidUpdate(); assert.equal(disposals, 1);
    plugin.destructor(); assert.equal(dispatched.filter((x) => x.kind === 'remove').length, 2);
});

test('UI does not contain the removed range or format-specific controls', () => {
    for (const file of ['api.ts', 'dialog.tsx', 'filter-editor.tsx']) {
        const text = fs.readFileSync(path.join(source, file), 'utf8');
        for (const removed of ['InputNumber', 'omitTags', 'omit_tags', 'tag_ids', 'ITGformat']) assert.ok(!text.includes(removed), `${file}: ${removed}`);
    }
    assert.match(fs.readFileSync(path.join(source, 'filter-editor.tsx'), 'utf8'), /@react-awesome-query-builder\/antd/);
});

test('all plugin files transpile without syntax errors', () => {
    for (const file of fs.readdirSync(source).filter((f) => /\.tsx?$/.test(f))) {
        const result = ts.transpileModule(fs.readFileSync(path.join(source, file), 'utf8'), {
            fileName: file, reportDiagnostics: true,
            compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
        });
        assert.deepEqual((result.diagnostics || []).filter((d) => d.category === ts.DiagnosticCategory.Error), []);
    }
});

test('editor uses supported read-only settings and does not update the display-filter store', () => {
    const code = fs.readFileSync(path.join(source, 'filter-editor.tsx'), 'utf8');
    for (const setting of ['immutableGroupsMode', 'immutableFieldsMode', 'immutableOpsMode', 'immutableValuesMode']) {
        assert.ok(code.includes(`${setting}: disabled`));
    }
    assert.ok(code.includes('if (!disabled) change(next)'));
    assert.ok(!code.includes('changeAnnotationsFilters'));
    assert.ok(!code.includes('dispatch'));
});
