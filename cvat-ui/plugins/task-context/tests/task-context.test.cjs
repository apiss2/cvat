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
    const filename = path.join(source, name);
    const result = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        fileName: filename, reportDiagnostics: true,
        compilerOptions: {
            target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.React, esModuleInterop: true,
        },
    });
    assert.deepEqual((result.diagnostics || []).filter((item) => item.category === ts.DiagnosticCategory.Error), []);
    const module = { exports: {} };
    vm.runInNewContext(result.outputText, {
        module, exports: module.exports, AbortController,
        require: (id) => {
            if (id in mocks) return mocks[id];
            throw new Error(`Unexpected import: ${id}`);
        },
        ...globals,
    }, { filename });
    return module.exports;
}

const { createAPI, validateDraft, errorMessage } = load('api.ts');
const result = (extra = {}) => ({ task_id: 7, fields: [], notes: '', version: 0, can_edit: true, ...extra });
const task = { type: 'tasks', id: 7, taskId: 7 };
const draft = { fields: [{ name: '撮影場所', value: '東京\n第二撮影室' }], notes: '追加情報' };

test('task context is read and replaced with an explicit organization and version', async () => {
    const requests = [];
    const api = createAPI(async (request) => {
        requests.push(request);
        return { data: result() };
    }, '/api/', task, 'team', () => 'owner');
    await api.read();
    await api.save(draft, 3);
    assert.equal(requests[0].method, 'GET');
    assert.equal(requests[0].url, '/api/task-context/tasks/7/');
    assert.equal(requests[0].data, undefined);
    assert.equal(requests[1].method, 'PUT');
    assert.equal(requests[1].url, '/api/task-context/tasks/7/');
    assert.deepEqual(plain(requests[1].data), { ...draft, version: 3 });
    for (const request of requests) {
        assert.deepEqual(plain(request.params), { org: 'team' });
        assert.equal(request.headers.Accept, 'application/vnd.cvat+json');
        assert.equal(request['axios-retry'].retries, 0);
    }
});

test('job views read through job authorization and save against the shared task', async () => {
    const requests = [];
    const api = createAPI(async (request) => {
        requests.push(request);
        return { data: result({ can_edit: false }) };
    }, '/api', { type: 'jobs', id: 100, taskId: 7 }, '', () => 'owner');
    assert.equal((await api.read()).can_edit, false);
    await api.save(draft, 0);
    assert.equal(requests[0].url, '/api/task-context/jobs/100/');
    assert.equal(requests[1].url, '/api/task-context/tasks/7/');
    assert.equal(requests[0].params.org, '');
});

test('a response for another task or with malformed permissions is rejected', async () => {
    for (const data of [result({ task_id: 8 }), result({ can_edit: 'true' }), result({ version: -1 }),
        result({ fields: [{}] }), null]) {
        const api = createAPI(async () => ({ data }), '/api', task, '', () => 'owner');
        await assert.rejects(api.read());
    }
});

test('invalid targets and versions fail before a request', async () => {
    let calls = 0;
    const send = async () => { calls++; return { data: result() }; };
    for (const target of [{ ...task, id: -1 }, { ...task, id: 8 }, { ...task, type: 'projects' },
        { ...task, taskId: NaN }, { ...task, id: 0.5 }]) {
        assert.throws(() => createAPI(send, '/api', target, '', () => 'owner'));
    }
    const api = createAPI(send, '/api', task, '', () => 'owner');
    for (const version of [-1, 0.5, NaN, Number.MAX_SAFE_INTEGER + 1]) {
        await assert.rejects(api.save(draft, version));
    }
    assert.equal(calls, 0);
});

test('conflicts and failed writes are not retried or rebased automatically', async () => {
    for (const reason of [{ response: { status: 409 } }, { response: { status: 403 } }, new Error('timeout')]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; throw reason; }, '/api', task, '', () => 'owner');
        await assert.rejects(api.save(draft, 0), (error) => error === reason);
        assert.equal(calls, 1);
    }
});

test('identity changes invalidate a session permanently, including personal workspace changes', async () => {
    let owner = 'user:personal';
    let calls = 0;
    const api = createAPI(async () => { calls++; return { data: result() }; }, '/api', task, '', () => owner);
    owner = 'user:organization';
    await assert.rejects(api.read());
    owner = 'user:personal';
    await assert.rejects(api.read());
    assert.equal(calls, 0);
});

test('responses and errors arriving after identity changes are discarded', async () => {
    for (const rejected of [false, true]) {
        let owner = 'a';
        let resolve;
        let reject;
        const api = createAPI(() => new Promise((yes, no) => { resolve = yes; reject = no; }),
            '/api', task, '', () => owner);
        const pending = api.read();
        owner = 'b';
        if (rejected) reject(new Error('old request'));
        else resolve({ data: result() });
        await assert.rejects(pending, /画面を開き直/);
    }
});

test('unmount disposal aborts requests and prevents later use', async () => {
    let request;
    const api = createAPI(async (value) => { request = value; return { data: result() }; },
        '/api', task, '', () => 'owner');
    await api.read();
    api.dispose();
    assert.equal(request.signal.aborted, true);
    await assert.rejects(api.save(draft, 0));
});

test('validation supports empty values and notes-only data but rejects blank or duplicate names', () => {
    for (const value of [draft, { fields: [], notes: 'メモ' }, { fields: [{ name: '番号', value: '' }], notes: '' }]) {
        assert.equal(validateDraft(value), '');
    }
    for (const fields of [[{ name: ' ', value: '1' }],
        [{ name: ' A ', value: '1' }, { name: 'A', value: '2' }],
        [{ name: 'N'.repeat(129), value: '' }], [{ name: 'A', value: 'v'.repeat(4097) }],
        Array.from({ length: 65 }, (_, index) => ({ name: String(index), value: '' }))]) {
        assert.notEqual(validateDraft({ fields, notes: '' }), '');
    }
    assert.notEqual(validateDraft({ fields: [], notes: 'n'.repeat(16385) }), '');
});

test('backend validation details and authorization errors remain readable', () => {
    assert.equal(errorMessage({ response: { status: 400, data: { detail: 'Size limit' } } }), 'Size limit');
    assert.match(errorMessage({ response: { status: 403 } }), /権限/);
});

test('registration is idempotent, uses the Info and task slots, and unsubscribes on removal', () => {
    let builder;
    let ready;
    let registrations = 0;
    let openedFor = '';
    let apiCreations = 0;
    const actions = [];
    const state = { auth: { user: { id: 1 } }, organizations: { current: { id: 2, slug: 'team' } } };
    const React = {
        createElement: (type, props, ...children) => ({ type, props, children }),
        useCallback: (callback) => callback,
        useEffect: () => {},
        useState: () => [openedFor, (next) => { openedFor = next; }],
        useSyncExternalStore: (_subscribe, snapshot) => snapshot(),
    };
    load('index.tsx', {
        '@modules/react': { __esModule: true, default: React, ...React },
        '@modules/axios': { __esModule: true, default: { request: () => {} } },
        '@ant-design/icons': { InfoCircleOutlined: 'InfoIcon' },
        'antd/lib/button': { __esModule: true, default: 'Button' },
        'antd/lib/modal': { __esModule: true, default: 'Modal' },
        './api': { createAPI: () => { apiCreations++; return {}; } },
        './panel': { __esModule: true, default: 'Panel' },
    }, { window: {
        cvatUI: { registerComponent: (value) => { builder = value; registrations++; } },
        addEventListener: (_name, callback) => { ready = callback; },
    } });
    ready();
    assert.equal(registrations, 1);
    const plugin = builder({ core: { config: { backendAPI: '/api' } },
        store: { getState: () => state, subscribe: () => () => {} },
        dispatch: (action) => actions.push(action), actionCreators: {
            addUIComponent: (slot, component) => ({ type: 'add', slot, component }),
            removeUIComponent: (slot, component) => ({ type: 'remove', slot, component }),
        } });
    assert.deepEqual(actions.map((action) => action.slot), [
        'taskPage.details.topBar.extras', 'annotationPage.info.sections',
    ]);
    const Info = actions[1].component;
    const infoProps = { targetProps: { jobInstance: { id: 100, taskId: 7 }, visible: false } };
    assert.equal(Info(infoProps), null);
    assert.equal(apiCreations, 0);
    infoProps.targetProps.visible = true;
    const rendered = Info(infoProps);
    assert.equal(rendered.props.taskId, 7);
    rendered.props.createAPI();
    assert.equal(apiCreations, 1);
    state.auth.user = null;
    assert.equal(Info(infoProps), null);
    plugin.destructor();
    assert.deepEqual(actions.filter((action) => action.type === 'remove').map((action) => action.slot), [
        'taskPage.details.topBar.extras', 'annotationPage.info.sections',
    ]);
});
