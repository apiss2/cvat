// SPDX-License-Identifier: MIT
// Run with DICOM_TEST_BUILD pointing to tsc's CommonJS output for api.ts/selection.ts.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { join, resolve } = require('node:path');
const { createAPI } = require(join(resolve(process.env.DICOM_TEST_BUILD || '.dicom-test-build'), 'api.js'));
const CHUNK = 16 * 1024 ** 2;
const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
    let resolve;
    const promise = new Promise((yes) => { resolve = yes; });
    return { promise, resolve };
}
function httpError(status, detail) {
    return Object.assign(new Error('HTTP failure'), { response: { status, data: { detail } } });
}
function virtualFile(size, name, slices = []) {
    return {
        name, size,
        arrayBuffer() { throw new Error('Whole-file reads are forbidden'); },
        slice(start, end) {
            assert.ok(end - start <= CHUNK);
            assert.ok(start >= 0 && end <= size);
            slices.push({ start, end });
            return { size: end - start, start, end };
        },
    };
}
function server({ before, after, chunkSize = CHUNK, concurrent = 4 } = {}) {
    const state = { calls: [], sessions: new Map(), active: 0, maxActive: 0, enqueued: 0 };
    const send = async (config) => {
        state.calls.push(config);
        assert.equal(config['axios-retry'].retries, 0);
        assert.equal(config.params.org, 'org-a');
        assert.equal(config.signal.aborted, false);
        await before?.(config, state);
        if (config.method === 'GET') {
            assert.equal(config.url, '/api/server/annotation/formats');
            return { data: { importers: [{ name: 'DICOM 1.0', enabled: true }] } };
        }
        assert.equal(config.method, 'POST');
        assert.equal(config.url, '/api/projects/42/dataset');
        assert.equal(config.params.format, 'DICOM 1.0');
        assert.equal(config.params.location, 'local');
        const op = config.params.dicom_upload;
        assert.equal(config.headers['Content-Type'], op === 'chunk' ? 'application/octet-stream' : 'application/json');
        const data = config.data;
        const id = op === 'chunk' ? config.params.upload_id : data.upload_id;
        assert.match(id, /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/);
        let session = state.sessions.get(id);
        let result;
        if (op === 'start') {
            if (!session) {
                session = { entries: [], offsets: [], declared: data, requestID: undefined };
                state.sessions.set(id, session);
            } else assert.deepEqual(data, session.declared);
            result = { upload_id: id, chunk_size: chunkSize, parallel_uploads: concurrent,
                max_files_per_batch: 500, max_upload_bytes: 1024 ** 4, max_upload_files: 100000 };
        } else {
            assert.ok(session, 'Every operation must use a previously created upload session');
            if (op === 'files') {
                assert.ok(data.files.length <= 500);
                assert.equal(data.start_index % 500, 0);
                if (data.start_index < session.entries.length) {
                    assert.deepEqual(session.entries.slice(data.start_index, data.start_index + data.files.length), data.files);
                } else {
                    assert.equal(data.start_index, session.entries.length);
                    session.entries.push(...data.files);
                    session.offsets.push(...data.files.map(() => 0));
                }
                result = { registered_files: session.entries.length };
            } else if (op === 'chunk') {
                const index = Number(config.params.file_index);
                const offset = Number(config.params.offset);
                const end = offset + data.size;
                assert.equal(session.entries.length, session.declared.file_count);
                assert.ok(data.size > 0 && data.size <= chunkSize);
                assert.ok(end <= session.entries[index].size);
                assert.ok(offset === session.offsets[index] || end <= session.offsets[index], 'Chunks must be ordered or exact retries');
                state.maxActive = Math.max(state.maxActive, ++state.active);
                try {
                    config.onUploadProgress?.({ loaded: data.size });
                    await tick();
                    if (config.signal.aborted) throw new Error('Request aborted');
                    session.offsets[index] = Math.max(session.offsets[index], end);
                    result = { offset: end };
                } finally { state.active--; }
            } else if (op === 'status') {
                result = { registered_files: session.entries.length, state: session.requestID ? 'queued' : 'uploading',
                    ...(session.requestID ? { rq_id: session.requestID } : {}) };
            } else if (op === 'finish') {
                assert.equal(session.entries.length, session.declared.file_count);
                assert.deepEqual(session.offsets, session.entries.map((file) => file.size));
                if (!session.requestID) {
                    session.requestID = `native:${id}`;
                    state.enqueued++;
                }
                result = { rq_id: session.requestID };
            } else if (op === 'cancel') {
                if (session.requestID) throw httpError(409, 'Import already queued');
                state.sessions.delete(id);
                result = { state: 'cancelled' };
            } else assert.fail(`Unexpected operation: ${op}`);
        }
        await after?.(config, result, state);
        return { data: result };
    };
    return { ...state, state, send, api: createAPI(send, '/api', 42, 'org-a', () => 'identity') };
}

test('native upload preserves relative paths, prefix, organization and raw file bytes', async () => {
    const current = server();
    const file = new File([new Uint8Array([0, 1, 2, 255])], '検査.dcm');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/検査.dcm' });
    await current.api.available();
    const id = await current.api.start([file], 'GRD');
    assert.ok(id.startsWith('native:'));
    const start = current.calls.find((call) => call.params.dicom_upload === 'start');
    assert.equal(start.data.prefix, 'GRD');
    assert.equal(start.data.total_bytes, 4);
    assert.equal(start.data.file_count, 1);
    const files = current.calls.find((call) => call.params.dicom_upload === 'files');
    assert.deepEqual(files.data.files, [{ path: 'folder/検査.dcm', size: 4 }]);
    const chunk = current.calls.find((call) => call.params.dicom_upload === 'chunk');
    assert.ok(chunk.data instanceof Blob);
    assert.deepEqual(new Uint8Array(await chunk.data.arrayBuffer()), new Uint8Array([0, 1, 2, 255]));
    assert.equal(current.calls.some((call) => call.data instanceof FormData), false);
    assert.equal(current.state.enqueued, 1);
});

test('200 GiB folder streams bounded slices with four workers and offsets above ZIP32 limits', async () => {
    const current = server();
    const slices = [];
    const files = Array.from({ length: 4 }, (_, index) => virtualFile(50 * 1024 ** 3, `${index}.dcm`, slices));
    const progress = [];
    await current.api.start(files, 'GRD', (value) => progress.push(value));
    assert.equal(current.state.maxActive, 4);
    assert.equal(slices.length, 200 * 1024 ** 3 / CHUNK);
    assert.ok(slices.some(({ start }) => start > 2 ** 32));
    assert.equal(Math.max(...slices.map(({ start, end }) => end - start)), CHUNK);
    const last = progress.at(-1);
    assert.equal(last.stage, 'accepted');
    assert.equal(last.totalBytes, 200 * 1024 ** 3);
    assert.equal(last.confirmedBytes, last.totalBytes);
    assert.equal(last.transferredBytes, last.totalBytes);
    assert.equal(last.completedFiles, 4);
    assert.ok(progress.every((value) => value.confirmedBytes <= value.transferredBytes && value.transferredBytes <= value.totalBytes));
    assert.equal(current.state.enqueued, 1);
});

test('file metadata is registered in bounded pages rather than one large JSON document', async () => {
    const current = server();
    const files = Array.from({ length: 1001 }, (_, index) => new File([], `${index}.dcm`));
    await current.api.start(files, 'GRD');
    const pages = current.calls.filter((call) => call.params.dicom_upload === 'files');
    assert.deepEqual(pages.map((call) => [call.data.start_index, call.data.files.length]), [[0, 500], [500, 500], [1000, 1]]);
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'chunk').length, 0);
});

test('input errors are rejected before opening a server session', async () => {
    const current = server();
    const valid = new File(['x'], 'image.dcm');
    await assert.rejects(current.api.start([valid], ''), /接頭辞/);
    await assert.rejects(current.api.start([], 'GRD'), /選択/);
    await assert.rejects(current.api.start([valid, new File(['y'], 'IMAGE.DCM')], 'GRD'), /重複/);
    for (const size of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1, Infinity]) {
        await assert.rejects(current.api.start([virtualFile(size, 'bad.dcm')], 'GRD'), /サイズ/);
    }
    assert.equal(current.calls.length, 0);
    await current.api.start([valid], 'GRD');
});

test('a lost chunk response retries the same bytes without counting them twice', async () => {
    let lost = false;
    const current = server({ chunkSize: 3, after(config) {
        if (config.params.dicom_upload === 'chunk' && !lost) { lost = true; throw new Error('Response lost'); }
    } });
    const progress = [];
    await current.api.start([new File(['1234567'], 'a.dcm')], 'GRD', (value) => progress.push(value));
    const chunks = current.calls.filter((call) => call.params.dicom_upload === 'chunk');
    assert.deepEqual(chunks.map((call) => call.params.offset), ['0', '0', '3', '6']);
    assert.equal(await chunks[0].data.text(), await chunks[1].data.text());
    assert.ok(progress.every((value) => value.transferredBytes <= 7 && value.confirmedBytes <= 7));
    assert.equal(progress.at(-1).confirmedBytes, 7);
});

test('temporary failures stop after bounded retries and the same dialog resumes confirmed offsets', async () => {
    let unavailable = true;
    const current = server({ chunkSize: 3, before(config) {
        if (config.params.dicom_upload === 'chunk' && config.params.offset === '3' && unavailable) {
            throw httpError(503);
        }
    } });
    const files = [new File(['1234567'], 'a.dcm')];
    await assert.rejects(current.api.start(files, 'GRD'), /転送を再開/);
    assert.deepEqual(current.calls.filter((call) => call.params.dicom_upload === 'chunk').map((call) => call.params.offset), ['0', '3', '3', '3', '3']);
    assert.equal(current.state.enqueued, 0);
    unavailable = false;
    await current.api.start(files, 'GRD');
    assert.deepEqual(current.calls.filter((call) => call.params.dicom_upload === 'chunk').map((call) => call.params.offset), ['0', '3', '3', '3', '3', '3', '6']);
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'start').length, 1);
    assert.equal(current.state.enqueued, 1);
});

test('acceptance is distinct from byte transfer and waits for the request receipt', async () => {
    const finish = deferred();
    const entered = deferred();
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'finish') { entered.resolve(); return finish.promise; }
    } });
    const progress = [];
    let settled = false;
    const pending = current.api.start([new File(['123'], 'a.dcm')], 'GRD', (value) => progress.push(value)).then(() => { settled = true; });
    await entered.promise;
    assert.equal(settled, false);
    assert.equal(progress.at(-1).stage, 'accepting');
    assert.equal(progress.at(-1).confirmedBytes, 3);
    assert.equal(progress.some((value) => value.stage === 'accepted'), false);
    finish.resolve();
    await pending;
    assert.equal(progress.at(-1).stage, 'accepted');
});

test('a lost finish response retries one receipt and never enqueues duplicate imports', async () => {
    let lost = false;
    const current = server({ after(config) {
        if (config.params.dicom_upload === 'finish' && !lost) { lost = true; throw new Error('Receipt lost'); }
    } });
    const files = [new File(['data'], 'a.dcm')];
    const id = await current.api.start(files, 'GRD');
    const count = current.calls.length;
    assert.equal(await current.api.start(files, 'GRD'), id);
    assert.equal(current.calls.length, count);
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'finish').length, 2);
    assert.equal(current.state.enqueued, 1);
});

test('manual receipt recovery uses server status and does not send another import', async () => {
    let loseReceipts = true;
    const current = server({ after(config) {
        if (config.params.dicom_upload === 'finish' && loseReceipts) throw httpError(502);
    } });
    const files = [new File(['data'], 'a.dcm')];
    await assert.rejects(current.api.start(files, 'GRD'), /受付を再確認/);
    assert.equal(current.state.enqueued, 1);
    const before = current.calls.length;
    loseReceipts = false;
    const id = await current.api.start(files, 'GRD');
    assert.ok(id.startsWith('native:'));
    assert.deepEqual(current.calls.slice(before).map((call) => call.params.dicom_upload), ['status']);
    assert.equal(current.state.enqueued, 1);
});

test('idempotent start survives a lost session creation response', async () => {
    let lost = false;
    const current = server({ after(config) {
        if (config.params.dicom_upload === 'start' && !lost) { lost = true; throw new Error('Response lost'); }
    } });
    await current.api.start([new File(['data'], 'a.dcm')], 'GRD');
    const requests = current.calls.filter((call) => call.params.dicom_upload === 'start');
    assert.equal(requests.length, 2);
    assert.equal(requests[0].data.upload_id, requests[1].data.upload_id);
    assert.equal(current.sessions.size, 1);
});

test('failed status recovery preserves the acceptance stage and its retry message', async () => {
    let unavailable = true;
    const current = server({
        before(config) {
            if (config.params.dicom_upload === 'status' && unavailable) throw httpError(503);
        },
        after(config, result) {
            if (config.params.dicom_upload === 'finish') result.rq_id = '';
        },
    });
    const files = [new File(['data'], 'a.dcm')];
    await assert.rejects(current.api.start(files, 'GRD'), /受付を再確認/);
    const progress = [];
    await assert.rejects(current.api.start(files, 'GRD', (value) => progress.push(value)), /受付を再確認/);
    assert.ok(progress.length > 0);
    assert.equal(progress.every((value) => value.stage === 'accepting' && value.confirmedBytes === 4), true);
    assert.equal(current.state.enqueued, 1);
    unavailable = false;
    await current.api.start(files, 'GRD');
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'finish').length, 1);
    assert.equal(current.state.enqueued, 1);
});

test('permission denial is not retried and stops every concurrent upload before finalization', async () => {
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'chunk' && config.params.file_index === '0') throw httpError(403);
    } });
    await assert.rejects(current.api.start(Array.from({ length: 8 }, (_, index) => new File(['data'], `${index}.dcm`)), 'GRD'), /権限/);
    assert.ok(current.calls.filter((call) => call.params.dicom_upload === 'chunk').length <= 4);
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'chunk' && call.params.file_index === '0').length, 1);
    assert.equal(current.state.active, 0);
    assert.equal(current.state.enqueued, 0);
});

test('server capacity errors are shown without retries', async () => {
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'start') throw httpError(413, 'Configured DICOM upload limit exceeded');
    } });
    await assert.rejects(current.api.start([new File(['data'], 'a.dcm')], 'GRD'), /Configured DICOM upload limit exceeded/);
    assert.equal(current.calls.length, 1);
});

test('a session cannot be resumed with a different selection or prefix', async () => {
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'chunk') throw httpError(400, 'Interrupted');
    } });
    const files = [new File(['data'], 'a.dcm')];
    await assert.rejects(current.api.start(files, 'GRD'), /Interrupted/);
    await assert.rejects(current.api.start([new File(['changed'], 'a.dcm')], 'GRD'), /同じファイル/);
    await assert.rejects(current.api.start(files, 'OTHER'), /同じファイル/);
});

test('concurrent submission calls are rejected without opening a second session', async () => {
    const gate = deferred();
    const entered = deferred();
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'start') { entered.resolve(); return gate.promise; }
    } });
    const files = [new File(['data'], 'a.dcm')];
    const first = current.api.start(files, 'GRD');
    await entered.promise;
    await assert.rejects(current.api.start(files, 'GRD'), /進行中/);
    gate.resolve();
    await first;
    assert.equal(current.sessions.size, 1);
});

test('changed identity rejects before and after HTTP and cancels upload ownership', async () => {
    let identity = 'a', count = 0;
    const api = createAPI(async () => { count++; identity = 'b'; return { data: {} }; }, '/api', 1, '', () => identity);
    await assert.rejects(api.available(), /変更/);
    await assert.rejects(api.available(), /変更/);
    assert.equal(count, 1);
    assert.equal(api.signal.aborted, true);
});

test('identity changes during progress abort remaining transfers without throwing from an event callback', async () => {
    let identity = 'a';
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'chunk') identity = 'b';
    } });
    const api = createAPI(current.send, '/api', 42, 'org-a', () => identity);
    await assert.rejects(api.start([new File(['data'], 'a.dcm')], 'GRD'), /変更/);
    assert.equal(api.signal.aborted, true);
    assert.equal(current.state.enqueued, 0);
});

test('disabled server format is rejected', async () => {
    const api = createAPI(async () => ({ data: { importers: [{ name: 'DICOM 1.0', enabled: false }] } }), '/api', 1, '', () => 'a');
    await assert.rejects(api.available(), /有効/);
});

test('dispose aborts active requests and prevents future operations', async () => {
    const gate = deferred();
    const entered = deferred();
    let signal;
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'chunk') { signal = config.signal; entered.resolve(); return gate.promise; }
    } });
    const files = [new File(['data'], 'a.dcm')];
    const pending = current.api.start(files, 'GRD');
    await entered.promise;
    current.api.dispose();
    assert.equal(signal.aborted, true);
    gate.resolve();
    await assert.rejects(pending, /中止/);
    const calls = current.calls.length;
    await assert.rejects(current.api.start(files, 'GRD'), /中止/);
    assert.equal(current.calls.length, calls);
    assert.equal(current.state.enqueued, 0);
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'cancel').length, 1);
    assert.equal(current.sessions.size, 0);
});

test('closing after acceptance never cancels the background import', async () => {
    const current = server();
    await current.api.start([new File(['data'], 'a.dcm')], 'GRD');
    const calls = current.calls.length;
    current.api.dispose();
    await tick();
    assert.equal(current.calls.length, calls);
    assert.equal(current.state.enqueued, 1);
});

test('closing after an identity change never sends cancellation under another account or organization', async () => {
    let identity = 'a';
    const current = server({ before(config) {
        if (config.params.dicom_upload === 'chunk') throw httpError(400, 'Interrupted');
    } });
    const api = createAPI(current.send, '/api', 42, 'org-a', () => identity);
    await assert.rejects(api.start([new File(['data'], 'a.dcm')], 'GRD'));
    const calls = current.calls.length;
    identity = 'b';
    api.dispose();
    await tick();
    assert.equal(current.calls.length, calls);
    assert.equal(api.signal.aborted, true);
});

test('identity is rechecked before deferred cancellation and a throwing transport cannot block close', async () => {
    for (const changed of [false, true]) {
        let identity = 'a', cancellations = 0;
        const current = server({ before(config) {
            if (config.params.dicom_upload === 'chunk') throw httpError(400, 'Interrupted');
        } });
        const api = createAPI((config) => {
            if (config.params.dicom_upload === 'cancel') { cancellations++; throw new Error('Transport unavailable'); }
            return current.send(config);
        }, '/api', 42, 'org-a', () => identity);
        await assert.rejects(api.start([new File(['data'], 'a.dcm')], 'GRD'));
        assert.doesNotThrow(() => api.dispose());
        if (changed) identity = 'b';
        await tick();
        assert.equal(cancellations, changed ? 0 : 1);
        assert.equal(api.signal.aborted, true);
    }
});

test('best-effort cancellation cannot delete an import whose receipt was not confirmed', async () => {
    const current = server({ after(config, result) {
        if (config.params.dicom_upload === 'finish') result.rq_id = '';
    } });
    await assert.rejects(current.api.start([new File(['data'], 'a.dcm')], 'GRD'), /受付を再確認/);
    assert.equal(current.state.enqueued, 1);
    current.api.dispose();
    await tick();
    assert.equal(current.calls.filter((call) => call.params.dicom_upload === 'cancel').length, 1);
    assert.equal(current.sessions.size, 1);
    assert.equal(current.state.enqueued, 1);
    const calls = current.calls.length;
    current.api.dispose();
    assert.equal(current.calls.length, calls);
});
