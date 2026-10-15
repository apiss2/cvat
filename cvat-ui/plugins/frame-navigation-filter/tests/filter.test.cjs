// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

function load(file, dependencies = {}) {
    const module = { exports: {} };
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    // Keep modules in one JS realm, like the webpack bundle (core uses instanceof Object).
    const factory = vm.runInThisContext(`(function(module, exports, require) {${output}\n})`, { filename: file });
    factory(module, module.exports, (name) => {
        if (!(name in dependencies)) throw new Error(`Unexpected dependency: ${name}`);
        return dependencies[name];
    });
    return module.exports;
}
const root = path.resolve(__dirname, '../../../..');
const { createNavigationFilterPlugin } = load(path.join(__dirname, '../src/ts/filter.ts'));

async function fixture() {
    const Registry = load(path.join(root, 'cvat-core/src/plugins.ts'), {
        './exceptions': { ArgumentError: TypeError },
    }).default;
    const objects = [{ type: 'tag' }, { type: 'mask' }, { type: 'polygon', hidden: true }];
    const calls = [];
    const filters = [{ '==': [{ var: 'type' }, 'tag'] }];
    const prototype = {
        annotations: {
            async get(frame, allTracks = false, selected = []) {
                return Registry.apiWrapper.call(this, prototype.annotations.get, frame, allTracks, selected);
            },
            async search(from, to, params) {
                return Registry.apiWrapper.call(this, prototype.annotations.search, from, to, params);
            },
        },
    };
    prototype.annotations.get.implementation = async (frame, allTracks, selected) => {
        if (!Array.isArray(selected)) throw new TypeError('Filters must be an array');
        if (!Number.isInteger(frame) || frame < 0) throw new TypeError('Invalid frame');
        calls.push({ frame, allTracks, selected });
        return selected.length ? objects.slice(0, 1) : objects;
    };
    prototype.annotations.search.implementation = async (from, to, params) => {
        assert.strictEqual(params.annotationsFilters, filters);
        return to;
    };
    const job = { annotations: {} };
    Object.entries(prototype.annotations).forEach(([key, fn]) => { job.annotations[key] = fn.bind(job); });
    let enabled = true;
    const plugin = createNavigationFilterPlugin(() => enabled);
    await Registry.register.call({ classes: { Job: { prototype } } }, plugin);
    assert.equal(plugin.functions.length, 1, 'the real core registry must bind exactly the Job.get hook');
    return { job, objects, calls, filters, disable: () => { enabled = false; } };
}

test('filtered display includes tags, masks and manually hidden states without changing state', async () => {
    const { job, objects, calls, filters } = await fixture();
    const saved = JSON.stringify(filters);
    assert.strictEqual(await job.annotations.get(5, true, filters), objects);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].frame, 5);
    assert.equal(calls[0].allTracks, true);
    assert.equal(calls[0].selected.length, 0);
    assert.equal(objects[2].hidden, true);
    assert.equal(JSON.stringify(filters), saved);
});

test('forward and backward search receive the original filters', async () => {
    const { job, filters } = await fixture();
    assert.equal(await job.annotations.search(5, 10, { annotationsFilters: filters }), 10);
    assert.equal(await job.annotations.search(5, 0, { annotationsFilters: filters }), 0);
});

test('unfiltered get does not recurse and disabling restores upstream filtering', async () => {
    const { job, calls, filters, disable } = await fixture();
    assert.equal((await job.annotations.get(0)).length, 3);
    assert.equal(calls.length, 1);
    disable();
    assert.equal((await job.annotations.get(0, false, filters)).length, 1);
});

test('core argument errors are not bypassed', async () => {
    const { job, filters } = await fixture();
    await assert.rejects(job.annotations.get(-1, false, filters), /Invalid frame/);
    await assert.rejects(job.annotations.get(0, false, null), /Filters must be an array/);
});

test('new annotations remain in the result after a refresh', async () => {
    const { job, objects, filters } = await fixture();
    await job.annotations.get(0, false, filters);
    objects.push({ type: 'mask', source: 'manual' });
    assert.equal((await job.annotations.get(0, false, filters)).length, 4);
});
