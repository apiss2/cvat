// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

const file = path.join(__dirname, '../src/ts/measurement.ts');
const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    fileName: file,
    reportDiagnostics: true,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
assert.deepEqual(output.diagnostics, [], `Cannot transpile ${file}`);
const loaded = { exports: {} };
const factory = vm.runInThisContext(`(function(module, exports) {${output.outputText}\n})`, { filename: file });
factory(loaded, loaded.exports);
const { parsePixelSpacing, resolveSpacing, polylineLength, formatLength } = loaded.exports;

const mm = (x = 0.3, y = 0.4) => ({ x, y, unit: 'mm' });
const tag = (value = '0.4\\0.3', frame = 100, extra = {}) => ({
    objectType: 'tag', frame, label: { name: 'DicomTag', attributes: [{ id: 17, name: '00280030' }] },
    attributes: { 17: value }, ...extra,
});

test('DICOM PixelSpacing stores row before column, and accepts decimal/scientific DS values', () => {
    assert.deepEqual(parsePixelSpacing('0.4\\0.3'), mm());
    assert.deepEqual(parsePixelSpacing(' +4.E-1 \\ .3 '), mm());
    assert.deepEqual(parsePixelSpacing('1\\2'), mm(2, 1));
});

test('spacing rejects non-DS, missing, nonpositive and non-finite values', () => {
    const invalid = [null, undefined, [0.4, 0.3], 0.4, '', ' ', '0.4', '0.4,0.3', '[0.4,0.3]',
        '0.4\\0.3\\1', '0.4\\', '\\0.3', '0.4\\0', '0\\0.3', '-0.4\\0.3',
        'NaN\\0.3', 'Infinity\\0.3', '0x10\\0.3', '0.4mm\\0.3', '1e309\\0.3', '1e-999\\0.3'];
    invalid.forEach((value) => assert.equal(parsePixelSpacing(value), null, String(value)));
});

test('only a current-frame DicomTag tag with the exact numeric tag attribute provides spacing', () => {
    assert.deepEqual(resolveSpacing([tag()], 100), mm());
    assert.equal(resolveSpacing([tag()], 99), null);
    assert.equal(resolveSpacing([tag('0.8\\0.6', 101)], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, { objectType: 'shape' })], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'Other', attributes: [{ id: 17, name: '00280030' }] },
    })], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'DicomTag', attributes: [{ id: 17, name: 'PixelSpacing' }] },
    })], 100), null);
    assert.deepEqual(resolveSpacing([tag('0.8\\0.6', 101), tag()], 100), mm());
});

test('missing metadata never falls back to a label default, another frame or another tag number', () => {
    assert.equal(resolveSpacing([], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, { attributes: {} })], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'DicomTag', attributes: [{ id: 17, name: '00280030', defaultValue: '0.4\\0.3' }] },
        attributes: {},
    })], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'DicomTag', attributes: [{ id: 17, name: '00181164' }] },
    })], 100), null);
});

test('matching duplicate tags are accepted, but conflicting, missing or malformed duplicates are not', () => {
    assert.deepEqual(resolveSpacing([tag(), tag('4e-1\\3e-1')], 100), mm());
    for (const other of [tag('0.5\\0.3'), tag('broken'), tag(''), tag(undefined, 100, { attributes: {} })]) {
        assert.equal(resolveSpacing([tag(), other], 100), null);
        assert.equal(resolveSpacing([other, tag()], 100), null);
    }
});

test('invalid frame numbers and ambiguous PixelSpacing attribute definitions are rejected', () => {
    for (const frame of [-1, 0.5, NaN, Infinity, '100', Number.MAX_SAFE_INTEGER + 1]) {
        assert.equal(resolveSpacing([tag()], frame), null);
    }
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'DicomTag', attributes: [{ name: '00280030' }] },
    })], 100), null);
    assert.equal(resolveSpacing([tag(undefined, 100, {
        label: { name: 'DicomTag', attributes: [{ id: 17, name: '00280030' }, { id: 18, name: '00280030' }] },
        attributes: { 17: '0.4\\0.3', 18: '0.4\\0.3' },
    })], 100), null);
});

test('isotropic spacing measures image coordinates with fractional vertices', () => {
    assert.equal(polylineLength([0, 0, 3, 4], mm(0.5, 0.5)), 2.5);
    assert.equal(polylineLength([0.25, 0.25, 3.25, 4.25], mm(0.5, 0.5)), 2.5);
});

test('anisotropic spacing scales each coordinate before computing each segment length', () => {
    assert.equal(polylineLength([0, 0, 10, 0], mm()), 3);
    assert.equal(polylineLength([0, 0, 0, 10], mm()), 4);
    assert.equal(polylineLength([0, 0, 10, 10], mm()), 5);
    assert.equal(polylineLength([0, 0, 10, 0, 10, 10], mm()), 7);
});

test('polyline remains open and repeated vertices add no length', () => {
    assert.equal(polylineLength([0, 0, 10, 0, 10, 0, 10, 10], mm()), 7);
    assert.equal(polylineLength([5, 5, 5, 5], mm()), 0);
    assert.equal(polylineLength([0, 0, 10, 0, 10, 10, 0, 0], mm()), 12);
});

test('translation and vertex reversal preserve physical length without modifying geometry', () => {
    const points = Object.freeze([0, 0, 10, 0, 10, 10]);
    const spacing = Object.freeze(mm());
    assert.equal(polylineLength(points, spacing), 7);
    assert.equal(polylineLength([10, 10, 10, 0, 0, 0], spacing), 7);
    assert.equal(polylineLength([300, 200, 310, 200, 310, 210], spacing), 7);
    assert.deepEqual(points, [0, 0, 10, 0, 10, 10]);
});

test('pixel measurements remain available without applicable spacing', () => {
    assert.equal(polylineLength([0, 0, 3, 4]), 5);
    assert.equal(polylineLength([0, 0, 3, 4], null), 5);
    assert.equal(formatLength(polylineLength([0, 0, 3, 4]), 'px'), '5 px');
});

test('invalid geometry, invalid spacing and overflow do not produce displayable measurements', () => {
    for (const points of [[], [1, 2], [0, 0, 3], [0, 0, NaN, 1], [0, 0, Infinity, 1], [0, 0, '3', 4], null]) {
        assert.equal(polylineLength(points, mm()), null);
    }
    assert.equal(polylineLength([0, 0, 3, 4], { ...mm(), x: 0 }), null);
    assert.equal(polylineLength([0, 0, Number.MAX_VALUE, Number.MAX_VALUE]), null);
    assert.equal(polylineLength([0, 0, 2, 2], mm(Number.MAX_VALUE, Number.MAX_VALUE)), null);
});

test('formatting is compact while preserving positive submillimeter measurements', () => {
    assert.equal(formatLength(7), '7 mm');
    assert.equal(formatLength(23.45), '23.45 mm');
    assert.equal(formatLength(23.456), '23.46 mm');
    assert.equal(formatLength(0), '0 mm');
    assert.equal(formatLength(0.00314159265), '0.003142 mm');
    assert.equal(formatLength(1e-10), '1e-10 mm');
    assert.equal(formatLength(Number.MIN_VALUE), '5e-324 mm');
    for (const value of [null, undefined, NaN, Infinity, -Infinity, -1, '1']) {
        assert.equal(formatLength(value), null);
    }
    assert.equal(formatLength(1, 'cm'), null);
});
