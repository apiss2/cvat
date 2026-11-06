// SPDX-License-Identifier: MIT
// Run with DICOM_TEST_BUILD pointing to tsc's CommonJS output for selection.ts/api.ts.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { join, resolve } = require('node:path');
const { collectFiles, collectDroppedFiles, safePath } = require(join(
    resolve(process.env.DICOM_TEST_BUILD || '.dicom-test-build'), 'selection.js',
));

function fileEntry(name, data = 'dicom') {
    return {
        name, isFile: true, isDirectory: false,
        file(success) { success(new File([data], name)); },
    };
}
function directory(name, entries, batchSize = 100) {
    return {
        name, isFile: false, isDirectory: true,
        createReader() {
            let offset = 0;
            return { readEntries(success) {
                const batch = entries.slice(offset, offset + batchSize);
                offset += batch.length;
                success(batch);
            } };
        },
    };
}
function transfer(...entries) {
    return {
        items: entries.map((entry) => ({ kind: 'file', webkitGetAsEntry: () => entry })),
        files: [],
    };
}
function signal() { return new AbortController().signal; }

test('folder selection filters unrelated files and keeps nested paths and supported archives', async () => {
    const names = ['image.DCM', 'image.dicom', 'study.tar', 'study.TAR.GZ', 'study.tgz', 'study.zip', 'notes.txt'];
    const files = names.map((name) => {
        const file = new File(['contents'], name);
        Object.defineProperty(file, 'webkitRelativePath', { value: `folder/nested/${name}` });
        return file;
    });
    const selected = await collectFiles(files, signal());
    assert.deepEqual(selected.map((file) => file.name), names.slice(0, -1));
    assert.deepEqual(selected.map((file) => file.webkitRelativePath), names.slice(0, -1).map((name) => `folder/nested/${name}`));
});

test('DICOM signature finds extensionless files without reading their full contents', async () => {
    const data = new Uint8Array(140);
    data.set(new TextEncoder().encode('DICM'), 128);
    const file = new File([data], '00001');
    file.arrayBuffer = () => { throw new Error('Whole-file reads are unnecessary'); };
    const selected = await collectFiles([file, new File([new Uint8Array(140)], 'not-dicom')], signal());
    assert.deepEqual(selected, [file]);
});

test('dropped directories are recursively searched and equal basenames retain distinct paths', async () => {
    const source = transfer(directory('root', [
        directory('one', [fileEntry('image.dcm', 'one')]),
        directory('two', [directory('three', [fileEntry('image.dcm', 'two'), fileEntry('study.tar')])]),
        fileEntry('notes.txt'),
    ]));
    const selected = await collectDroppedFiles(source, signal());
    assert.deepEqual(selected.map((file) => file.webkitRelativePath), [
        'root/one/image.dcm', 'root/two/three/image.dcm', 'root/two/three/study.tar',
    ]);
    assert.equal(await selected[0].text(), 'one');
    assert.equal(await selected[1].text(), 'two');
});

test('all directory batches are read, including more than 100 files', async () => {
    const source = transfer(directory('root', Array.from({ length: 205 }, (_, i) => fileEntry(`${i}.dcm`))));
    const selected = await collectDroppedFiles(source, signal());
    assert.equal(selected.length, 205);
    assert.equal(selected[204].webkitRelativePath, 'root/204.dcm');
});

test('drop entries are captured synchronously before the event data is protected', async () => {
    let protectedData = false;
    const source = {
        get items() {
            assert.equal(protectedData, false);
            return [{ kind: 'file', webkitGetAsEntry() {
                assert.equal(protectedData, false);
                return directory('root', [fileEntry('a.dcm')]);
            } }];
        },
        get files() { throw new Error('Entry support must take precedence over files'); },
    };
    const result = collectDroppedFiles(source, signal());
    protectedData = true;
    assert.equal((await result)[0].webkitRelativePath, 'root/a.dcm');
});

test('plain file drops fall back without directory-entry support and ignore text items', async () => {
    const file = new File(['data'], 'a.dcm');
    const itemSource = {
        items: [{ kind: 'string' }, { kind: 'file', getAsFile: () => file }], files: [],
    };
    assert.deepEqual(await collectDroppedFiles(itemSource, signal()), [file]);
    assert.deepEqual(await collectDroppedFiles({ items: [], files: [file] }, signal()), [file]);
    for (const value of [null, undefined]) {
        const unavailable = {
            items: [{ kind: 'file', webkitGetAsEntry: () => value, getAsFile: () => value }], files: [file],
        };
        assert.deepEqual(await collectDroppedFiles(unavailable, signal()), [file]);
    }
});

test('mixed dropped files and folders are collected together', async () => {
    const file = new File(['archive'], 'study.tar');
    const source = transfer(directory('root', [fileEntry('a.dcm')]));
    source.items.push({ kind: 'file', webkitGetAsEntry: () => null, getAsFile: () => file });
    const selected = await collectDroppedFiles(source, signal());
    assert.equal(selected.length, 2);
    assert.equal(selected[0].webkitRelativePath, 'root/a.dcm');
    assert.equal(selected[1], file);
});

test('empty directories and unsupported files produce no selected input', async () => {
    assert.deepEqual(await collectDroppedFiles(transfer(directory('root', [directory('empty', [])])), signal()), []);
    assert.deepEqual(await collectFiles([new File(['notes'], 'notes.txt')], signal()), []);
});

test('aborted selection stops before access and after a pending directory read', async () => {
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(collectFiles([], controller.signal), /中止/);
    await assert.rejects(collectDroppedFiles({ get items() { throw new Error('Access after abort'); } }, controller.signal), /中止/);

    const active = new AbortController();
    let finishRead;
    const root = {
        name: 'root', isDirectory: true, isFile: false,
        createReader: () => ({ readEntries(success) { finishRead = success; } }),
    };
    const pending = collectDroppedFiles(transfer(root), active.signal);
    active.abort();
    finishRead([fileEntry('a.dcm')]);
    await assert.rejects(pending, /中止/);
});

test('folder and file access errors are surfaced without a partial selection', async () => {
    const root = {
        name: 'root', isDirectory: true, isFile: false,
        createReader: () => ({ readEntries(success, failure) { failure(new Error('Permission denied')); } }),
    };
    await assert.rejects(collectDroppedFiles(transfer(root), signal()), /Permission denied/);
    const file = {
        name: 'a.dcm', isDirectory: false, isFile: true,
        file(success, failure) { failure(new Error('Missing file')); },
    };
    await assert.rejects(collectDroppedFiles(transfer(file), signal()), /Missing file/);
});

test('large inputs are collected without the former ZIP size and file-count limits', async () => {
    const large = { name: 'large.dcm', size: 200 * 1024 ** 3,
        slice() { throw new Error('A named DICOM need not be read during selection'); } };
    assert.deepEqual(await collectFiles([large], signal()), [large]);
    const file = new File([], 'a.dcm');
    assert.equal((await collectFiles(Array(50001).fill(file), signal())).length, 50001);
    for (const size of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1, NaN, Infinity]) {
        await assert.rejects(collectFiles([{ name: 'bad.dcm', size }], signal()), /サイズ/);
    }
});

test('unsafe relative paths are rejected before transfer', async () => {
    const file = new File(['dicom'], 'a.dcm');
    Object.defineProperty(file, 'webkitRelativePath', { value: '../a.dcm' });
    await assert.rejects(collectFiles([file], signal()), /安全でない/);
    await assert.rejects(collectDroppedFiles(transfer(directory('..', [fileEntry('a.dcm')])), signal()), /安全でない/);
});

test('safe paths preserve UTF-8 and reject traversal, drive paths, empty components and controls', () => {
    assert.equal(safePath('検査/画像.dcm'), '検査/画像.dcm');
    for (const value of ['../a', '/a', 'C:/a', 'a\\b', 'a//b', './a', 'a\0b', '', 'a/']) {
        assert.throws(() => safePath(value));
    }
    assert.throws(() => safePath('検'.repeat(342)), /長すぎ/);
});
