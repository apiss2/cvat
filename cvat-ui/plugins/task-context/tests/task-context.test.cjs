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

const { createAPI, errorMessage } = load('api.ts');
const guide = (extra = {}) => ({ id: 11, task_id: 7, project_id: null, markdown: '# 撮影情報\n\n撮影場所: 東京', ...extra });
const task = { type: 'tasks', id: 7, taskId: 7 };
const job = { type: 'jobs', id: 100, taskId: 7 };

test('task information reads the standard task guide without creating or saving anything', async () => {
    const requests = [];
    const api = createAPI(async (request) => {
        requests.push(request);
        return { data: requests.length === 1 ? { id: 7, guide_id: 11 } : guide() };
    }, '/api/', task, 'team', () => 'owner');
    assert.deepEqual(plain(await api.read()), guide());
    assert.deepEqual(requests.map((request) => request.url), ['/api/tasks/7', '/api/guides/11']);
    assert.deepEqual(plain(requests[0].params), { org: 'team' });
    assert.equal(requests[1].params, undefined);
    for (const request of requests) {
        assert.equal(request.method, 'GET');
        assert.equal(request.data, undefined);
        assert.equal(request.headers.Accept, 'application/vnd.cvat+json');
        assert.equal(request['axios-retry'].retries, 0);
    }
    assert.equal(api.save, undefined);
});

test('job information selects task_guide_id and never the existing project guide_id', async () => {
    const requests = [];
    const api = createAPI(async (request) => {
        requests.push(request);
        return { data: requests.length === 1 ? { id: 100, task_id: 7, task_guide_id: 11, guide_id: 99 } : guide() };
    }, '/api', job, '', () => 'owner');
    assert.equal((await api.read()).id, 11);
    assert.deepEqual(requests.map((request) => request.url), ['/api/jobs/100', '/api/guides/11']);
    assert.equal(requests[0].params.org, '');
    assert.equal(requests[1].params, undefined);
});

test('missing task guides remain empty and never fall back to the project guide', async () => {
    for (const [target, resource] of [[task, { id: 7, guide_id: null }],
        [job, { id: 100, task_id: 7, guide_id: 99, task_guide_id: null }]]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; return { data: resource }; }, '/api', target, '', () => 'owner');
        assert.equal(await api.read(), null);
        assert.equal(calls, 1);
    }
});

test('wrong resources and missing task_guide_id are rejected before guide retrieval', async () => {
    for (const [target, resource] of [[task, { id: 8, guide_id: 11 }],
        [job, { id: 100, task_id: 8, task_guide_id: 11 }],
        [job, { id: 100, task_id: 7, guide_id: 99 }],
        [task, { id: 7, guide_id: '11' }], [task, { id: 7, guide_id: 0 }], [task, null]]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; return { data: resource }; }, '/api', target, '', () => 'owner');
        await assert.rejects(api.read());
        assert.equal(calls, 1);
    }
});

test('guides for another task or a project are never shown as this task information', async () => {
    for (const invalid of [guide({ task_id: 8 }), guide({ project_id: 2 }), guide({ id: 12 }),
        guide({ markdown: 42 }), null]) {
        let calls = 0;
        const api = createAPI(async () => ({ data: ++calls === 1 ? { id: 7, guide_id: 11 } : invalid }),
            '/api', task, '', () => 'owner');
        await assert.rejects(api.read());
    }
});

test('invalid targets fail before a request', () => {
    let calls = 0;
    const send = async () => { calls++; return { data: null }; };
    for (const target of [{ ...task, id: -1 }, { ...task, id: 8 }, { ...task, type: 'projects' },
        { ...task, taskId: NaN }, { ...task, id: 0.5 }]) {
        assert.throws(() => createAPI(send, '/api', target, '', () => 'owner'));
    }
    assert.equal(calls, 0);
});

test('an identity change invalidates the session permanently', async () => {
    let owner = 'user:personal';
    let calls = 0;
    const api = createAPI(async () => { calls++; return { data: null }; }, '/api', task, '', () => owner);
    owner = 'user:organization';
    await assert.rejects(api.read());
    owner = 'user:personal';
    await assert.rejects(api.read());
    assert.equal(calls, 0);
});

test('a session change during task retrieval prevents the follow-up guide request', async () => {
    let owner = 'a';
    let finish;
    let calls = 0;
    const api = createAPI(() => { calls++; return new Promise((resolve) => { finish = resolve; }); },
        '/api', task, '', () => owner);
    const pending = api.read();
    owner = 'b';
    finish({ data: { id: 7, guide_id: 11 } });
    await assert.rejects(pending, /画面を開き直/);
    assert.equal(calls, 1);
});

test('responses and failures from a previous session are discarded during guide retrieval', async () => {
    for (const rejected of [false, true]) {
        let owner = 'a';
        let calls = 0;
        let resolve;
        let reject;
        let started;
        const guideStarted = new Promise((ready) => { started = ready; });
        const api = createAPI(() => {
            calls++;
            if (calls === 1) return Promise.resolve({ data: { id: 7, guide_id: 11 } });
            return new Promise((yes, no) => { resolve = yes; reject = no; started(); });
        }, '/api', task, '', () => owner);
        const pending = api.read();
        await guideStarted;
        owner = 'b';
        if (rejected) reject(new Error('old request'));
        else resolve({ data: guide() });
        await assert.rejects(pending, /画面を開き直/);
    }
});

test('unmount disposal aborts requests and prevents later use', async () => {
    let request;
    const api = createAPI(async (value) => { request = value; return { data: { id: 7, guide_id: null } }; },
        '/api', task, '', () => 'owner');
    await api.read();
    api.dispose();
    assert.equal(request.signal.aborted, true);
    await assert.rejects(api.read());
});

test('read errors are preserved and never trigger writes or retries', async () => {
    for (const reason of [{ response: { status: 403 } }, { response: { status: 404 } }, new Error('timeout')]) {
        let calls = 0;
        const api = createAPI(async () => { calls++; throw reason; }, '/api', task, '', () => 'owner');
        await assert.rejects(api.read(), (error) => error === reason);
        assert.equal(calls, 1);
    }
    assert.equal(errorMessage({ response: { status: 400, data: { detail: 'Invalid request' } } }), 'Invalid request');
    assert.match(errorMessage({ response: { status: 403 } }), /権限/);
});

test('only task details link to the standard editor; closed Info and anonymous sessions stay inactive', () => {
    let builder;
    let ready;
    let registrations = 0;
    let openedFor = '';
    let apiCreations = 0;
    const actions = [];
    const navigations = [];
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
        '@modules/react-router': { useHistory: () => ({ push: (path) => navigations.push(path) }) },
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
    assert.equal(rendered.props.onEdit, undefined);
    rendered.props.createAPI();
    assert.equal(apiCreations, 1);
    const TaskButton = actions[0].component;
    const taskProps = { targetProps: { task: { id: 7 } } };
    const closed = TaskButton(taskProps);
    assert.equal(closed.children[1].props.open, false);
    closed.children[0].props.onClick();
    const opened = TaskButton(taskProps);
    assert.equal(opened.children[1].props.open, true);
    opened.children[1].children[0].props.onEdit();
    assert.deepEqual(navigations, ['/tasks/7/guide']);
    state.auth.user = null;
    assert.equal(Info(infoProps), null);
    assert.equal(TaskButton(taskProps), null);
    plugin.destructor();
    assert.deepEqual(actions.filter((action) => action.type === 'remove').map((action) => action.slot), [
        'taskPage.details.topBar.extras', 'annotationPage.info.sections',
    ]);
});
