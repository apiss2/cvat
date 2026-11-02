// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const { load, tick } = require('./helpers.cjs');

function deferred() {
    let resolve; let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function plugin({ setupError } = {}) {
    const pending = deferred();
    const notifications = []; const progress = []; const listeners = [];
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
            submitted = accepted; close = closed;
            return { focus: () => {}, dispose: closed };
        } },
    }, { window: {
        cvatUI: { registerComponent: (value) => { builder = value; } },
        addEventListener: () => {},
    } });
    const instance = builder({
        core: { config: { backendAPI: '/api' }, requests: { listen: (id, options) => {
            if (setupError) throw setupError;
            listeners.push({ id, options }); return pending.promise;
        } } },
        store: { getState: () => state }, dispatch: () => {},
        actionCreators: {
            addUIComponent: (name, component) => {
                assert.equal(name, 'projectActions.items'); item = component; return {};
            },
            removeUIComponent: () => ({}),
        },
    });
    const menu = item({ targetProps: { projectInstance: { id: 9, name: 'Study', dimension: '2d' } } });
    assert.equal(menu.label, 'Import annotations');
    menu.onClick();
    return { pending, notifications, progress, listeners, state, instance,
        submit: (id = 'native-request') => submitted(id), close: () => close() };
}

for (const [message, kind, description] of [
    ['Project annotations: 2 tasks succeeded; 0 tasks failed; 0 unmatched records', 'success',
        '成功: 2 Task、失敗: 0 Task、未一致: 0 件'],
    ['WARNING: Project annotations: 1 tasks succeeded; 1 tasks failed; 2 unmatched records; 3 invalid records', 'warning',
        '成功: 1 Task、失敗: 1 Task、未一致: 2 件、入力不備: 3 件'],
    ['Project annotations: 1 tasks succeeded; 0 tasks failed; 1 unmatched records', 'warning',
        '成功: 1 Task、失敗: 0 Task、未一致: 1 件'],
]) {
    test(`completion (${message}) reports safe counts after closing the dialog`, async () => {
        const current = plugin();
        current.submit(); await tick();
        assert.equal(current.listeners.length, 1);
        const update = { status: 'started', message: 'Importing' };
        current.listeners[0].options.callback(update);
        assert.deepEqual(current.progress, [update]);
        current.close();
        current.pending.resolve({ status: 'finished', message }); await tick();
        assert.equal(current.notifications.length, 1);
        assert.equal(current.notifications[0].kind, kind);
        assert.equal(current.notifications[0].description, description);
    });
}

test('all-task failure reports error counts', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.pending.reject(new Error('Project annotations: 0 tasks succeeded; 2 tasks failed; 1 unmatched records'));
    await tick();
    assert.equal(current.notifications[0].kind, 'error');
    assert.equal(current.notifications[0].description, '成功: 0 Task、失敗: 2 Task、未一致: 1 件');
});

test('an unexpected server error is not copied into notifications', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.pending.reject(new Error('PatientID confidential')); await tick();
    assert.equal(current.notifications[0].kind, 'error');
    assert.doesNotMatch(JSON.stringify(current.notifications), /PatientID|confidential/);
});

test('cancelled requests are not reported as successful', async () => {
    const current = plugin();
    current.submit(); await tick();
    current.pending.resolve({ status: 'canceled', message: 'Cancelled' }); await tick();
    assert.equal(current.notifications[0].kind, 'info');
    assert.match(current.notifications[0].message, /中止/);
});

for (const changed of ['user', 'organization', 'destructor']) {
    test(`${changed} invalidates earlier progress and result ownership`, async () => {
        const current = plugin();
        current.submit(); await tick();
        if (changed === 'user') current.state.auth.user.id = 7;
        else if (changed === 'organization') current.state.organizations.current.id = 8;
        else current.instance.destructor();
        current.instance.globalStateDidUpdate();
        current.state.auth.user.id = 1; current.state.organizations.current.id = 2;
        current.instance.globalStateDidUpdate();
        current.listeners[0].options.callback({ status: 'started', message: 'private' });
        current.pending.resolve({ status: 'finished', message: 'Project annotations: 1 tasks succeeded; 0 tasks failed; 0 unmatched records' });
        await tick();
        assert.deepEqual(current.progress, []); assert.deepEqual(current.notifications, []);
    });
}

test('changing and restoring the account before listener setup does not revive ownership', async () => {
    const current = plugin();
    current.state.auth.user.id = 7; current.instance.globalStateDidUpdate();
    current.state.auth.user.id = 1; current.instance.globalStateDidUpdate();
    current.submit(); await tick();
    assert.deepEqual(current.listeners, []);
});

test('listener setup failures are asynchronous and preserve accepted uploads', async () => {
    const current = plugin({ setupError: new Error('network with private detail') });
    assert.doesNotThrow(() => current.submit()); await tick();
    assert.equal(current.notifications[0].kind, 'error');
    assert.doesNotMatch(current.notifications[0].description, /private/);
});
