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

function dialogHarness({ start, accepted = () => {} } = {}) {
    let mounted;
    const values = [];
    const effects = [];
    const listeners = new Map();
    let stateIndex = 0, effectIndex = 0, disposed = 0;
    const selected = new File(['dicom'], 'study.dcm');
    const React = {
        createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
        useRef: () => ({ current: null }),
        useMemo: (calculate) => calculate(),
        useEffect: (effect, dependencies) => {
            const index = effectIndex++;
            const previous = effects[index];
            if (!previous || previous.dependencies.some((value, position) => value !== dependencies[position])) {
                previous?.cleanup?.();
                effects[index] = { dependencies, cleanup: effect() };
            }
        },
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
        'react-dom/client': { createRoot: () => ({ render: (node) => { mounted = node; },
            unmount: () => { for (const effect of effects) effect?.cleanup?.(); } }) },
        '@ant-design/icons': {}, antd,
        './selection': {},
    }, { document: { createElement: () => ({ remove: () => {} }), body: { appendChild: () => {} } },
        window: { addEventListener: (name, listener) => listeners.set(name, listener),
            removeEventListener: (name, listener) => { if (listeners.get(name) === listener) listeners.delete(name); } } });
    const defaultStart = async (files, prefix, progress) => {
        assert.deepEqual(files, [selected]);
        assert.equal(prefix, 'GRD');
        progress({ stage: 'accepted', transferredBytes: 5, confirmedBytes: 5, totalBytes: 5, completedFiles: 1, totalFiles: 1 });
        return 'accepted-request';
    };
    const handle = dialog.openDialog({ signal: new AbortController().signal, available: async () => {},
        start: start || defaultStart, dispose: () => { disposed++; } }, 'Study', () => {}, accepted);
    const render = () => {
        stateIndex = 0; effectIndex = 0;
        const tree = mounted.type();
        const nodes = [];
        function visit(node) {
            if (Array.isArray(node)) node.forEach(visit);
            else if (node && typeof node === 'object') { nodes.push(node); visit(node.children); visit(node.props?.footer); }
        }
        visit(tree);
        return { tree, nodes };
    };
    return { values, listeners, render, handle, selected,
        get disposed() { return disposed; },
        submit: () => render().nodes.find((node) => node.type === 'Button' && node.props.type === 'primary').props.onClick() };
}

test('a dialog callback error preserves the accepted request and distinguishes result confirmation', async () => {
    const current = dialogHarness({ accepted: () => { throw new Error('monitoring setup failed'); } });
    await current.submit(); await tick();
    assert.equal(current.values[5], true);
    assert.equal(current.values[8], 'accepted-request');
    assert.match(current.values[7], /^登録は受け付けましたが/);
    assert.equal(current.values[4], false);
    const { nodes } = current.render();
    assert.equal(nodes.some((node) => node.type === 'a'), false);
    assert.equal(nodes.find((node) => node.type === 'Alert' && node.props.type === 'success').props.description,
        '変換とタスク作成はサーバーで続行します。この画面を閉じても処理は続き、画面上部のRequestsから進捗を確認できます。');
});

test('dialog reports acknowledged bytes, separates acceptance and protects navigation until receipt', async () => {
    const finish = deferred();
    let publish;
    const current = dialogHarness({ start: async (_files, _prefix, progress) => {
        publish = progress;
        progress({ stage: 'uploading', transferredBytes: 200, confirmedBytes: 0,
            totalBytes: 200, completedFiles: 0, totalFiles: 1 });
        await finish.promise;
        return 'accepted-request';
    } });
    const pending = current.submit();
    await tick();
    let { tree, nodes } = current.render();
    assert.equal(nodes.find((node) => node.type === 'Progress').props.percent, 0);
    assert.equal(tree.props.closable, false);
    assert.equal(tree.props.keyboard, false);
    assert.equal(current.listeners.has('beforeunload'), true);
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    current.listeners.get('beforeunload')(event);
    assert.equal(event.prevented, true);
    assert.equal(event.returnValue, '');
    publish({ stage: 'uploading', transferredBytes: 200, confirmedBytes: 100,
        totalBytes: 200, completedFiles: 0, totalFiles: 1 });
    nodes = current.render().nodes;
    assert.equal(nodes.find((node) => node.type === 'Progress').props.percent, 50);
    publish({ stage: 'uploading', transferredBytes: 200, confirmedBytes: 200,
        totalBytes: 200, completedFiles: 1, totalFiles: 1 });
    assert.ok(current.render().nodes.find((node) => node.type === 'Progress').props.percent < 100);
    publish({ stage: 'accepting', transferredBytes: 200, confirmedBytes: 200,
        totalBytes: 200, completedFiles: 1, totalFiles: 1 });
    nodes = current.render().nodes;
    assert.equal(nodes.some((node) => node.type === 'Progress'), false);
    assert.equal(nodes.some((node) => node.type === 'Alert' && node.props.type === 'success'), false);
    assert.equal(current.listeners.has('beforeunload'), true);
    finish.resolve();
    await pending;
    nodes = current.render().nodes;
    assert.equal(nodes.some((node) => node.type === 'Alert' && node.props.type === 'success'), true);
    assert.equal(current.listeners.has('beforeunload'), false);
});

test('a failed transfer can be resumed while selected files and prefix remain locked', async () => {
    let count = 0;
    const current = dialogHarness({ start: async (files, prefix, progress) => {
        assert.deepEqual(files, [current.selected]);
        assert.equal(prefix, 'GRD');
        progress({ stage: 'uploading', transferredBytes: 3, confirmedBytes: 3,
            totalBytes: 5, completedFiles: 0, totalFiles: 1 });
        if (!count++) throw new Error('転送が中断しました。');
        return 'accepted-request';
    } });
    await current.submit();
    const nodes = current.render().nodes;
    const button = nodes.find((node) => node.type === 'Button' && node.props.type === 'primary');
    assert.equal(button.props.disabled, false);
    assert.ok(button.children.includes('転送を再開'));
    assert.equal(nodes.find((node) => node.type === 'Input').props.disabled, true);
    assert.equal(nodes.filter((node) => node.type === 'input').every((node) => node.props.disabled), true);
    await current.submit();
    assert.equal(current.values[8], 'accepted-request');
});

test('explicit stop closes the dialog, disposes transfer and removes its navigation guard', async () => {
    const finish = deferred();
    const current = dialogHarness({ start: async (_files, _prefix, progress) => {
        progress({ stage: 'uploading', transferredBytes: 0, confirmedBytes: 0,
            totalBytes: 5, completedFiles: 0, totalFiles: 1 });
        await finish.promise;
        throw new Error('Aborted');
    } });
    const pending = current.submit();
    await tick();
    const nodes = current.render().nodes;
    const close = nodes.find((node) => node.type === 'Button' && node.props.danger);
    assert.ok(close.children.includes('転送を中止して閉じる'));
    close.props.onClick();
    assert.equal(current.disposed, 1);
    assert.equal(current.listeners.has('beforeunload'), false);
    finish.resolve();
    await pending;
    current.handle.dispose();
    assert.equal(current.disposed, 1);
});
