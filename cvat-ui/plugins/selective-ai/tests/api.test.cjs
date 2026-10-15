// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const { createAPI } = require('./load.cjs')('api.ts');

test('adapter uses shared transport and pins organization on all requests', async () => {
    const calls = [];
    const api = createAPI(async (request) => { calls.push(request); return { data: { ok: true } }; }, '/api/', 'team', () => 'user1:team');
    assert.deepEqual(await api.get('/tasks', { page: 1 }), { ok: true });
    await api.infer(5, 'onnx-r1', { frame: 2, threshold: 0.5, mapping: { seg: { name: 'mask', attributes: {} } }, conv_mask_to_poly: false });
    await api.append(5, { tags: [], shapes: [{ frame: 2, label_id: 10 }], tracks: [] });
    assert.deepEqual(calls.map((c) => [c.method, c.url]), [
        ['GET', '/api/tasks'], ['POST', '/api/lambda/functions/onnx-r1'], ['PATCH', '/api/tasks/5/annotations'],
    ]);
    assert.ok(calls.every((c) => c.params.org === 'team'));
    assert.equal(calls[2].params.action, 'create'); assert.equal(calls[1].data.task, 5);
    assert.equal(calls[1].data.frame, 2); assert.equal('cleanup' in calls[1].data, false);
    assert.equal('Authorization' in calls[0], false, 'do not invent or persist credentials');
});

test('identity changes before a request prevent network calls and remain latched', async () => {
    let identity = 'user1:org1'; let sent = 0;
    const api = createAPI(async () => { sent++; return { data: {} }; }, '/api', 'org1', () => identity);
    identity = 'user2:org1'; await assert.rejects(api.get('/tasks'), /利用者または組織/);
    identity = 'user1:org1'; await assert.rejects(api.get('/tasks')); assert.equal(sent, 0);
});

test('identity changes during inference discard the response', async () => {
    let identity = 'u1';
    const api = createAPI(async () => { identity = 'u2'; return { data: {} }; }, '/api', '', () => identity);
    await assert.rejects(api.infer(5, 'model', { frame: 0 }), /利用者または組織/);
});

test('write failures are not retried by the adapter', async () => {
    let sent = 0; const api = createAPI(async () => { sent++; throw new Error('lost'); }, '/api', '', () => 'u1');
    await assert.rejects(api.append(5, { tags: [], shapes: [], tracks: [] }), /lost/); assert.equal(sent, 1);
});

test('relative path escape and foreign next URLs cannot be sent', async () => {
    let sent = 0; const api = createAPI(async () => { sent++; return { data: {} }; }, '/api', '', () => 'u1');
    for (const path of ['//evil.invalid/', 'https://evil.invalid', '/../tasks']) await assert.rejects(api.get(path));
    assert.throws(() => api.infer(5, '../requests', {})); assert.equal(sent, 0);
});
