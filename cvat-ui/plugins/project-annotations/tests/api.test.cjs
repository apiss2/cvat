// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const { load } = require('./helpers.cjs');
const { createAPI, selectFormats } = load('api.ts');
const { DICOM_FORMAT, readSummary, summaryMessage, errorMessage, ImportError } = load('messages.ts');
const imports = [
    { name: 'CVAT 1.1', enabled: true, dimension: '2d' },
    { name: 'COCO 1.0', enabled: true, dimension: '2d' },
    { name: 'DICOM 1.0', enabled: true, dimension: '2d' },
    { name: 'Task Guide JSON 1.0', enabled: true, dimension: '2d' },
    { name: 'Task Guide Markdown Zip 1.0', enabled: true, dimension: '2d' },
    { name: 'Task Guide DICOM Report Import 1.0', enabled: true, dimension: '2d' },
    { name: 'KITTI 1.0', enabled: true, dimension: '3d' },
    { name: 'Disabled 1.0', enabled: false, dimension: '2d' },
    { name: DICOM_FORMAT, enabled: true, dimension: '2d' },
];
const archive = () => new File(['zip content'], 'upload.zip');

test('format choices retain enabled task formats and exclude media/guide imports', () => {
    assert.deepEqual(selectFormats(imports, '2d'), [DICOM_FORMAT, 'COCO 1.0', 'CVAT 1.1']);
    assert.deepEqual(selectFormats(imports, '3d'), ['KITTI 1.0']);
    assert.deepEqual(selectFormats(imports, ''), [DICOM_FORMAT, 'COCO 1.0', 'CVAT 1.1', 'KITTI 1.0']);
});

for (const mode of ['append', 'replace']) {
    for (const format of ['COCO 1.0', DICOM_FORMAT]) {
        test(`${mode} ${format} posts once with project annotation parameters`, async () => {
            const requests = [];
            const api = createAPI(async (config) => {
                requests.push(config);
                return { data: config.method === 'GET' ? { importers: imports } : { rq_id: 'native:1' } };
            }, '/api', 42, 'org-a', () => 'owner', '2d');
            await api.formats();
            assert.equal(await api.start(archive(), format, mode), 'native:1');
            const request = requests[1];
            assert.equal(request.url, '/api/projects/42/annotations');
            assert.deepEqual(request.params, { org: 'org-a', format, import_mode: mode, location: 'local' });
            assert.equal(request.data.get('annotation_file').name, 'upload.zip');
            assert.equal(request.data.has('dataset_file'), false);
            assert.equal(request.timeout, 0);
            assert.deepEqual(request['axios-retry'], { retries: 0 });
            await assert.rejects(api.start(archive(), format, mode), /Requests/);
            assert.equal(requests.length, 2);
        });
    }
}

test('an uncertain POST cannot be retried and never exposes its error details', async () => {
    let posts = 0;
    const api = createAPI(async (config) => {
        if (config.method === 'GET') return { data: { importers: imports } };
        posts++;
        throw new Error('PatientID secret from server');
    }, '/api', 1, '', () => 'owner');
    await api.formats();
    await assert.rejects(api.start(archive(), DICOM_FORMAT, 'append'), (error) => {
        assert.match(error.message, /Requests/);
        assert.doesNotMatch(error.message, /secret|PatientID/);
        return true;
    });
    await assert.rejects(api.start(archive(), DICOM_FORMAT, 'append'), /送信済み/);
    assert.equal(posts, 1);
});

test('a disabled or absent extension refuses existing task formats too', async () => {
    for (const available of [imports.filter((item) => item.name !== DICOM_FORMAT),
        imports.map((item) => item.name === DICOM_FORMAT ? { ...item, enabled: false } : item)]) {
        const api = createAPI(async () => ({ data: { importers: available } }), '/api', 1, '', () => 'owner');
        await assert.rejects(api.formats(), /有効ではありません/);
        await assert.rejects(api.start(archive(), 'COCO 1.0', 'append'), /取り込み形式/);
    }
});

test('invalid file, format, and import mode fail before submitting and allow correction', async () => {
    let posts = 0;
    const api = createAPI(async ({ method }) => {
        if (method === 'GET') return { data: { importers: imports } };
        posts++; return { data: { rq_id: 'one' } };
    }, '/api', 1, '', () => 'owner', '2d');
    await api.formats();
    for (const file of [new File([], 'empty.zip'), new File(['data'], 'sample.json')]) {
        await assert.rejects(api.start(file, DICOM_FORMAT, 'append'), /ZIP/);
    }
    await assert.rejects(api.start(archive(), 'Task Guide JSON 1.0', 'append'), /取り込み形式/);
    await assert.rejects(api.start(archive(), 'COCO 1.0', 'invalid'), /取り込み形式/);
    assert.equal(posts, 0);
    assert.equal(await api.start(archive(), 'COCO 1.0', 'append'), 'one');
});

test('identity changes during HTTP invalidate returned formats and all later requests', async () => {
    let identity = 'one'; let calls = 0;
    const api = createAPI(async () => {
        calls++; identity = 'two'; return { data: { importers: imports } };
    }, '/api', 1, '', () => identity);
    await assert.rejects(api.formats(), /アカウント/);
    identity = 'one';
    await assert.rejects(api.formats(), /アカウント/);
    assert.equal(calls, 1);
});

test('dispose aborts an upload signal and prevents subsequent requests', async () => {
    let signal;
    const api = createAPI(async (request) => {
        signal = request.signal;
        return { data: request.method === 'GET' ? { importers: imports } : { rq_id: 'one' } };
    }, '/api', 1, '', () => 'owner');
    await api.formats();
    await api.start(archive(), DICOM_FORMAT, 'append');
    api.dispose();
    assert.equal(signal.aborted, true);
    await assert.rejects(api.formats(), /アカウント/);
});

test('summary notifications contain only parsed counts, never arbitrary surrounding text', () => {
    const message = 'secret PatientID: WARNING: Project annotations: 2 tasks succeeded; 1 tasks failed; 3 unmatched records; 4 invalid records; patient 001';
    const summary = readSummary(message);
    assert.deepEqual(summary, { succeeded: 2, failed: 1, unmatched: 3, invalid: 4 });
    assert.equal(summaryMessage(summary), '成功: 2 Task、失敗: 1 Task、未一致: 3 件、入力不備: 4 件');
    assert.equal(readSummary('Project annotations: names or patient IDs'), undefined);
    assert.equal(readSummary('Project annotations: 9007199254740992 tasks succeeded; 0 tasks failed; 0 unmatched records'), undefined);
    assert.doesNotMatch(errorMessage(new Error('PatientID secret')), /secret|PatientID/);
    assert.equal(errorMessage(new ImportError('Known fixed message')), 'Known fixed message');
});
