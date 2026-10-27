// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

function load(name, dependencies, globals = {}) {
    const file = path.resolve(__dirname, '../src/ts', name);
    const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: {
            target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.React, esModuleInterop: true,
        },
    });
    assert.deepEqual(compiled.diagnostics, []);
    const target = { exports: {} };
    const factory = vm.runInThisContext(`(function(module, exports, require, window, document) {${compiled.outputText}\n})`, { filename: file });
    factory(target, target.exports, (name) => {
        if (name in dependencies) return dependencies[name];
        throw Error(`Unexpected dependency: ${name}`);
    }, globals.window, globals.document);
    return target.exports;
}

function deferred() {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

function plugin({ setupError } = {}) {
    const pending = deferred();
    const notifications = [];
    const progress = [];
    const listeners = [];
    const state = { auth: { user: { id: 1 } }, organizations: { current: { id: 2, slug: 'team' } }, projects: { selected: [] } };
    let builder; let item; let submitted; let close;
    load('index.tsx', {
        '@modules/axios': { request: () => {} },
        'antd/lib/notification': Object.fromEntries(['success', 'warning', 'error', 'info'].map((kind) => [
            kind, (notice) => notifications.push({ kind, ...notice }),
        ])),
        'actions/requests-actions': { updateRequestProgress: (request) => progress.push(request) },
        './api': { createAPI: () => ({}) },
        './dialog': { openDialog: (_api, _name, closed, accepted) => {
            submitted = accepted;
            close = closed;
            return { focus: () => {}, dispose: closed };
        } },
    }, { window: {
        cvatUI: { registerComponent: (value) => { builder = value; } },
        addEventListener: () => {},
    } });
    const instance = builder({
        core: { config: { backendAPI: '/api' }, requests: { listen: (id, options) => {
            if (setupError) throw setupError;
            listeners.push({ id, options });
            return pending.promise;
        } } },
        store: { getState: () => state },
        dispatch: () => {},
        actionCreators: {
            addUIComponent: (_name, component) => { item = component; return {}; },
            removeUIComponent: () => ({}),
        },
    });
    item({ targetProps: { projectInstance: { id: 9, name: 'Study', dimension: '2d' } } }).onClick();
    return { pending, notifications, progress, listeners, state, instance,
        submit: (id = 'native-request') => submitted(id), close: () => close() };
}

for (const [message, kind] of [
    ['DICOM: 2 tasks created; 0 tasks failed', 'success'],
    ['WARNING: DICOM: 1 tasks created; 1 tasks failed', 'warning'],
]) {
    test(`native completion produces a ${kind} notification after the dialog closes`, async () => {
        const current = plugin();
        current.submit();
        await tick();
        assert.equal(current.listeners.length, 1);
        assert.equal(current.listeners[0].id, 'native-request');
        const update = { status: 'started', message: 'Converting DICOM' };
        current.listeners[0].options.callback(update);
        assert.deepEqual(current.progress, [update]);
        current.close();
        current.pending.resolve({ status: 'finished', message });
        await tick();
        assert.equal(current.notifications.length, 1);
        assert.equal(current.notifications[0].kind, kind);
        assert.equal(current.notifications[0].description, message);
    });
}

test('all-failed native requests produce an error notification', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.pending.reject(new Error('DICOM: 0 tasks created; 2 tasks failed'));
    await tick();
    assert.equal(current.notifications.length, 1);
    assert.equal(current.notifications[0].kind, 'error');
    assert.equal(current.notifications[0].description, 'DICOM: 0 tasks created; 2 tasks failed');
});

test('cancelled native requests are not reported as successful imports', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.pending.resolve({ status: 'canceled', message: 'Cancelled' });
    await tick();
    assert.equal(current.notifications.length, 1);
    assert.equal(current.notifications[0].kind, 'info');
    assert.match(current.notifications[0].message, /中止/);
});

for (const changed of ['user', 'organization', 'destructor']) {
    test(`${changed} invalidates progress and completion notification ownership`, async () => {
        const current = plugin();
        current.submit(); await tick();
        if (changed === 'user') current.state.auth.user.id = 7;
        else if (changed === 'organization') current.state.organizations.current.id = 8;
        else current.instance.destructor();
        current.instance.globalStateDidUpdate();
        // Returning to the original identity must not revive its previous subscription.
        current.state.auth.user.id = 1;
        current.state.organizations.current.id = 2;
        current.instance.globalStateDidUpdate();
        current.listeners[0].options.callback({ status: 'started', message: 'private' });
        current.pending.resolve({ status: 'finished', message: 'DICOM: counts' });
        await tick();
        assert.deepEqual(current.progress, []);
        assert.deepEqual(current.notifications, []);
    });
}

test('account change also suppresses a rejected request notification', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.state.auth.user = null;
    current.instance.globalStateDidUpdate();
    current.pending.reject(new Error('private result'));
    await tick();
    assert.deepEqual(current.notifications, []);
});

test('identity changes before the accepted callback cannot transfer or revive request ownership', async () => {
    for (const restore of [false, true]) {
        const current = plugin();
        current.state.auth.user.id = 7;
        current.instance.globalStateDidUpdate();
        if (restore) {
            current.state.auth.user.id = 1;
            current.instance.globalStateDidUpdate();
        }
        current.submit();
        await tick();
        assert.deepEqual(current.listeners, []);
        assert.deepEqual(current.notifications, []);
    }
});

test('listener setup failure is asynchronous and never rejects submission', async () => {
    const current = plugin({ setupError: new Error('Could not obtain status') });
    assert.doesNotThrow(() => current.submit());
    await tick();
    assert.equal(current.notifications.length, 1);
    assert.equal(current.notifications[0].kind, 'error');
    assert.equal(current.notifications[0].description, 'Could not obtain status');
});

test('a dialog callback error preserves the accepted request and distinguishes result confirmation', async () => {
    let mounted;
    const values = [];
    let stateIndex = 0;
    const selected = new File(['dicom'], 'study.dcm');
    const React = {
        createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
        useRef: () => ({ current: null }),
        useState: (initial) => {
            const index = stateIndex++;
            if (!(index in values)) values[index] = index === 0 ? [selected] : index === 1 ? 'GRD' : initial;
            return [values[index], (value) => { values[index] = value; }];
        },
    };
    const antd = Object.fromEntries(['Alert', 'Button', 'Form', 'Input', 'Modal', 'Progress', 'Space', 'Spin'].map((name) => [name, name]));
    antd.Form = { Item: 'Form.Item' };
    antd.theme = { useToken: () => ({ token: {} }) };
    antd.Typography = { Text: 'Text' };
    const dialog = load('dialog.tsx', {
        react: React,
        'react-dom/client': { createRoot: () => ({ render: (node) => { mounted = node; }, unmount: () => {} }) },
        '@ant-design/icons': {}, antd,
        './archive': { pack: async () => new File(['zip'], 'dicom.zip') },
        './selection': {},
    }, { document: { createElement: () => ({ remove: () => {} }), body: { appendChild: () => {} } } });
    dialog.openDialog({ signal: new AbortController().signal, available: async () => {},
        start: async () => 'accepted-request', dispose: () => {} }, 'Study', () => {},
    () => { throw new Error('monitoring setup failed'); });
    const tree = mounted.type();
    const submit = tree.props.footer.children.find((node) => node.props.type === 'primary').props.onClick;
    await submit(); await tick();
    assert.equal(values[5], true);
    assert.equal(values[8], 'accepted-request');
    assert.match(values[7], /^登録は受け付けましたが/);
    assert.equal(values[4], false);
    stateIndex = 0;
    const nodes = [];
    function visit(node) {
        if (Array.isArray(node)) node.forEach(visit);
        else if (node && typeof node === 'object') { nodes.push(node); visit(node.children); }
    }
    visit(mounted.type());
    assert.equal(nodes.some((node) => node.type === 'a'), false);
    assert.equal(nodes.find((node) => node.type === 'Alert' && node.props.type === 'success').props.description,
        'ダイアログを閉じた後、画面上部のRequestsから進捗を確認できます。');
});
