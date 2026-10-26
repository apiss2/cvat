// SPDX-License-Identifier: MIT
// Run with DICOM_TEST_BUILD pointing to tsc's CommonJS output for archive.ts/api.ts.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { execFileSync } = require('node:child_process');
const build = resolve(process.env.DICOM_TEST_BUILD || '.dicom-test-build');
const { pack, crc32, safePath } = require(join(build, 'archive.js'));
const { createAPI } = require(join(build, 'api.js'));

test('CRC32 known vector and incremental chunks', () => {
    const bytes = new TextEncoder().encode('123456789');
    assert.equal(crc32(bytes), 0xCBF43926);
    assert.equal(crc32(bytes.slice(4), crc32(bytes.slice(0, 4))), crc32(bytes));
});
test('ZIP is read by Python, with UTF-8, prefix and exact bytes', async () => {
    const controller = new AbortController();
    const file = new File([new Uint8Array([0, 1, 2, 255])], '検査.dcm');
    const archive = await pack([file], 'GRD', controller.signal);
    const dir = mkdtempSync(join(tmpdir(), 'dicom-zip-test-'));
    try {
        writeFileSync(join(dir, 'upload.zip'), Buffer.from(await archive.arrayBuffer()));
        execFileSync('python3', ['-c', `import zipfile,json,sys
with zipfile.ZipFile(sys.argv[1]) as z:
 assert z.testzip() is None
 assert z.read('検査.dcm') == bytes([0,1,2,255])
 assert json.loads(z.read('_cvat_dicom_import.json')) == {'version':1,'prefix':'GRD'}
`, join(dir, 'upload.zip')]);
    } finally { rmSync(dir, { recursive: true }); }
});
test('folder relative paths retained', async () => {
    const file = new File(['data'], 'a.dcm');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/a.dcm' });
    const archive = await pack([file], 'GRD', new AbortController().signal);
    assert.ok(Buffer.from(await archive.arrayBuffer()).includes(Buffer.from('folder/a.dcm')));
});
test('unsafe paths refused', () => {
    for (const value of ['../a', '/a', 'C:/a', 'a\\b', 'a//b', './a', 'a\0b']) {
        assert.throws(() => safePath(value));
    }
});
test('empty prefix, collisions and reserved manifest refused', async () => {
    const signal = new AbortController().signal;
    await assert.rejects(pack([new File(['x'], 'a')], '', signal));
    await assert.rejects(pack([new File(['x'], 'a'), new File(['y'], 'A')], 'GRD', signal));
    await assert.rejects(pack([new File(['x'], '_cvat_dicom_import.json')], 'GRD', signal));
});
test('abort prevents packaging', async () => {
    const control = new AbortController(); control.abort();
    await assert.rejects(pack([new File(['x'], 'a')], 'GRD', control.signal));
});
test('oversized upload refused without reading its bytes', async () => {
    const fake = { name: 'large', size: 2 * 1024 ** 3 + 1 };
    await assert.rejects(pack([fake], 'GRD', new AbortController().signal));
});
test('API uses native project endpoint, organization and no retries', async () => {
    const requests = [];
    const api = createAPI(async (config) => {
        requests.push(config);
        return { data: config.method === 'GET' ? { importers: [{ name: 'DICOM 1.0', enabled: true }] } : { rq_id: 'native:1' } };
    }, '/api', 42, 'org-a', () => 'identity');
    await api.available();
    assert.equal(await api.start(new File(['data'], 'upload.zip')), 'native:1');
    assert.equal(requests[1].url, '/api/projects/42/dataset');
    assert.equal(requests[1].params.org, 'org-a');
    assert.equal(requests[1]['axios-retry'].retries, 0);
    assert.equal(requests[1].data.get('dataset_file').name, 'upload.zip');
    await assert.rejects(api.start(new File(['data'], 'upload.zip')));
    assert.equal(requests.length, 2);
});
test('failed POST is not retried', async () => {
    let count = 0;
    const api = createAPI(async () => { count++; throw new Error('network'); }, '/api', 1, '', () => 'user');
    await assert.rejects(api.start(new File(['data'], 'upload.zip')), /Requests/);
    await assert.rejects(api.start(new File(['data'], 'upload.zip')), /Requests/);
    assert.equal(count, 1);
});
test('changed identity rejects before and after HTTP', async () => {
    let identity = 'a', count = 0;
    const api = createAPI(async () => { count++; identity = 'b'; return { data: {} }; }, '/api', 1, '', () => identity);
    await assert.rejects(api.available());
    await assert.rejects(api.available());
    assert.equal(count, 1);
});
test('disabled server format rejected', async () => {
    const api = createAPI(async () => ({ data: { importers: [{ name: 'DICOM 1.0', enabled: false }] } }), '/api', 1, '', () => 'a');
    await assert.rejects(api.available());
});
test('dispose aborts upload signal', async () => {
    let signal;
    const api = createAPI(async (request) => { signal = request.signal; return { data: { rq_id: 'id' } }; }, '/api', 1, '', () => 'a');
    await api.start(new File(['data'], 'upload.zip')); api.dispose();
    assert.equal(signal.aborted, true);
});
