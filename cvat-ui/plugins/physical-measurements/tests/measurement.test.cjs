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
const { parseMeasurementConfig, resolveSpacing, polylineLength, formatLength } = loaded.exports;

const mm = (x = 0.3, y = 0.4) => ({ x, y, unit: 'mm' });
const config = (overrides = {}) => ({ version: 1, default: mm(), frames: {}, ...overrides });

test('parsing preserves explicit frame exclusions and detaches caller objects', () => {
    const input = config({ frames: { 0: mm(0.5, 0.5), 17: null } });
    const parsed = parseMeasurementConfig(input);
    assert.deepEqual(parsed, input);
    input.default.x = 99;
    input.frames[0].y = 99;
    input.frames[17] = mm();
    assert.deepEqual(parsed.default, mm());
    assert.deepEqual(parsed.frames[0], mm(0.5, 0.5));
    assert.equal(parsed.frames[17], null);
});

test('an entirely uncalibrated task is valid', () => {
    const parsed = parseMeasurementConfig(config({ default: null }));
    assert.equal(resolveSpacing(parsed, 0), null);
    assert.equal(resolveSpacing(null, 0), null);
});

test('unknown, missing and unsupported configuration fields are rejected', () => {
    const invalid = [
        null, [], true, 'config', {},
        config({ version: 2 }), config({ version: '1' }),
        { version: 1, frames: {} }, { version: 1, default: null },
        config({ defaut: mm() }), config({ revision: 'server-token' }),
        config({ frames: [] }), config({ frames: null }), config({ frames: new Date() }),
    ];
    invalid.forEach((value) => assert.throws(() => parseMeasurementConfig(value), TypeError));
});

test('both spacing axes require positive finite numbers and exactly the supported unit', () => {
    for (const field of ['x', 'y']) {
        for (const value of [0, -0.1, NaN, Infinity, -Infinity, '0.3', true, null, undefined]) {
            const invalid = { ...mm(), [field]: value };
            assert.throws(() => parseMeasurementConfig(config({ default: invalid })), /default/);
            assert.throws(() => parseMeasurementConfig(config({ frames: { 9: invalid } })), /frames\[9\]/);
        }
    }
    for (const value of [{ x: 1, y: 1 }, { ...mm(), z: 1 }, { ...mm(), unit: 'cm' }, [0.3, 0.4]]) {
        assert.throws(() => parseMeasurementConfig(config({ default: value })), /default/);
    }
});

test('frame keys are canonical nonnegative safe integers, with no prototype keys', () => {
    for (const frame of ['-1', '1.2', '01', '00', '+1', ' 1', '1e2', '9007199254740992', '__proto__', 'constructor']) {
        const frames = Object.fromEntries([[frame, mm()]]);
        assert.throws(() => parseMeasurementConfig(config({ frames })), /Invalid task frame number/);
    }
    const parsed = parseMeasurementConfig(config({ frames: { 0: null, 9007199254740991: mm() } }));
    assert.deepEqual(resolveSpacing(parsed, Number.MAX_SAFE_INTEGER), mm());
});

test('task-relative frame overrides and exclusions take precedence without leaking across frames', () => {
    const parsed = parseMeasurementConfig(config({ frames: { 100: mm(1, 2), 101: null } }));
    assert.deepEqual(resolveSpacing(parsed, 99), mm());
    assert.deepEqual(resolveSpacing(parsed, 100), mm(1, 2));
    assert.equal(resolveSpacing(parsed, 101), null);
    assert.deepEqual(resolveSpacing(parsed, 102), mm());
    for (const frame of [-1, 0.5, NaN, Infinity, '100', Number.MAX_SAFE_INTEGER + 1]) {
        assert.equal(resolveSpacing(parsed, frame), null);
    }
});

test('a malformed runtime override cannot silently use the task calibration', () => {
    for (const override of [undefined, {}, { ...mm(), x: 0 }]) {
        assert.equal(resolveSpacing(config({ frames: { 5: override } }), 5), null);
    }
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
