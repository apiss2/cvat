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
        fileName: file,
        reportDiagnostics: true,
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    assert.deepEqual(output.diagnostics, [], `Cannot transpile ${file}`);
    // Keep modules in one JS realm, like the webpack bundle (core uses instanceof Object).
    const factory = vm.runInThisContext(`(function(module, exports, require) {${output.outputText}\n})`, { filename: file });
    factory(module, module.exports, (name) => {
        if (!(name in dependencies)) throw new Error(`Unexpected dependency: ${name}`);
        return dependencies[name];
    });
    return module.exports;
}
const root = path.resolve(__dirname, '../../../..');
const { createNavigationFilterPlugin } = load(path.join(__dirname, '../src/ts/filter.ts'));

function registry() {
    return load(path.join(root, 'cvat-core/src/plugins.ts'), { './exceptions': { ArgumentError: TypeError } }).default;
}

async function fixture(hooks, companionFirst = false, withNavigation = true) {
    const Registry = registry();
    const objects = [{ type: 'tag' }, { type: 'mask' }, { type: 'polygon', hidden: true }];
    const calls = [];
    const searches = [];
    const navigationCalls = [];
    const filters = Object.freeze([{ '==': [{ var: 'type' }, 'tag'] }]);
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
    Object.defineProperty(prototype.annotations.get, 'implementation', {
        value: async function get(frame, allTracks, selected) {
            calls.push({ context: this, frame, allTracks, selected });
            if (!Array.isArray(selected)) throw new TypeError('Filters must be an array');
            if (!Number.isInteger(frame) || frame < 0) throw new TypeError('Invalid frame');
            return selected.length ? objects.slice(0, 1) : objects;
        },
    });
    Object.defineProperty(prototype.annotations.search, 'implementation', {
        value: async function search(from, to, params) {
            searches.push({ from, to, params });
            assert.strictEqual(params.annotationsFilters, filters);
            return to;
        },
    });
    const job = { annotations: {} };
    Object.entries(prototype.annotations).forEach(([key, fn]) => { job.annotations[key] = fn.bind(job); });
    let enabled = true;
    const plugin = createNavigationFilterPlugin(() => enabled);
    const enter = plugin.cvat.classes.Job.prototype.annotations.get.enter;
    plugin.cvat.classes.Job.prototype.annotations.get.enter = function (...args) {
        navigationCalls.push(args);
        return enter.apply(this, args);
    };
    const api = { classes: { Job: { prototype } } };
    const registerCompanion = () => Registry.register.call(api, {
        name: 'Companion hook', description: 'Exercise one wrapper invocation',
        cvat: { classes: { Job: { prototype: { annotations: { get: hooks } } } } },
    });
    if (hooks && companionFirst) await registerCompanion();
    if (withNavigation) {
        await Registry.register.call(api, plugin);
        assert.equal(plugin.functions.length, 1, 'bind exactly the Job.get hook');
    }
    if (hooks && !companionFirst) await registerCompanion();
    return { job, objects, calls, searches, navigationCalls, filters, disable: () => { enabled = false; } };
}

test('filtered display includes all states without changing caller filters or hidden flags', async () => {
    const { job, objects, calls, navigationCalls, filters } = await fixture();
    const saved = JSON.stringify(filters);
    assert.strictEqual(await job.annotations.get(5, true, filters), objects);
    assert.equal(calls.length, 1);
    assert.equal(navigationCalls.length, 1);
    assert.strictEqual(calls[0].context, job);
    assert.equal(calls[0].frame, 5);
    assert.equal(calls[0].allTracks, true);
    assert.deepEqual(calls[0].selected, []);
    assert.equal(objects[2].hidden, true);
    assert.equal(JSON.stringify(filters), saved);
});

test('forward and backward search still receive the original filters after display', async () => {
    const { job, filters, searches } = await fixture();
    await job.annotations.get(5, false, filters);
    assert.equal(await job.annotations.search(5, 10, { annotationsFilters: filters }), 10);
    assert.equal(await job.annotations.search(5, 0, { annotationsFilters: filters }), 0);
    assert.equal(searches.length, 2);
});

test('unfiltered get is unchanged and disabling restores upstream filtering', async () => {
    const { job, calls, filters, disable } = await fixture();
    assert.equal((await job.annotations.get(0)).length, 3);
    disable();
    assert.equal((await job.annotations.get(0, false, filters)).length, 1);
    assert.equal(calls.length, 2);
    assert.strictEqual(calls[1].selected, filters);
});

test('without the navigation plugin existing filtering is unchanged', async () => {
    const { job, calls, filters } = await fixture(undefined, false, false);
    assert.equal((await job.annotations.get(0, false, filters)).length, 1);
    assert.strictEqual(calls[0].selected, filters);
});

test('new annotations remain in the result after a refresh', async () => {
    const { job, objects, filters } = await fixture();
    await job.annotations.get(0, false, filters);
    objects.push({ type: 'mask', source: 'manual' });
    assert.equal((await job.annotations.get(0, false, filters)).length, 4);
});

for (const companionFirst of [true, false]) {
    const order = `companion first: ${companionFirst}`;
    test(`each enter/leave runs once with original arguments (${order})`, async () => {
        const enterCalls = [];
        const leaveCalls = [];
        const { job, objects, calls, navigationCalls, filters } = await fixture({
            async enter(_plugin, frame, allTracks, filters) {
                enterCalls.push({ context: this, frame, allTracks, filters });
            },
            async leave(_plugin, result, frame, allTracks, filters) {
                leaveCalls.push({ context: this, frame, allTracks, filters });
                return result.map((state) => ({ ...state, visits: (state.visits || 0) + 1 }));
            },
        }, companionFirst);
        const result = await job.annotations.get(5, true, filters);
        assert.equal(calls.length, 1);
        assert.equal(navigationCalls.length, 1);
        assert.equal(enterCalls.length, 1);
        assert.equal(leaveCalls.length, 1);
        for (const call of [...enterCalls, ...leaveCalls]) {
            assert.strictEqual(call.context, job);
            assert.strictEqual(call.filters, filters);
            assert.equal(call.frame, 5);
            assert.equal(call.allTracks, true);
        }
        assert.ok(result.every((state) => state.visits === 1));
        assert.equal(result[2].hidden, true);
        assert.ok(objects.every((state) => !('visits' in state)));
    });

    for (const replace of [false, true]) {
        test(`companion can prevent the method before any read (replace: ${replace}, ${order})`, async () => {
            const replacement = [{ type: 'replacement' }];
            let enters = 0;
            let leaves = 0;
            const { job, calls, navigationCalls, filters } = await fixture({
                async enter() {
                    enters++;
                    return replace ? { preventMethodCallWithReturn: replacement } : { preventMethodCall: true };
                },
                async leave(_plugin, result) { leaves++; return result; },
            }, companionFirst);
            assert.strictEqual(await job.annotations.get(0, false, filters), replace ? replacement : null);
            assert.equal(calls.length, 0, 'no eager implementation call inside an enter hook');
            assert.equal(navigationCalls.length, 1);
            assert.equal(enters, 1);
            assert.equal(leaves, 1);
        });
    }

    test(`enter failure prevents the read without re-entry (${order})`, async () => {
        const failure = new Error('enter failed');
        let enters = 0;
        let leaves = 0;
        const { job, calls, filters } = await fixture({
            async enter() { enters++; throw failure; },
            async leave(_plugin, result) { leaves++; return result; },
        }, companionFirst);
        await assert.rejects(job.annotations.get(0, false, filters), (error) => error === failure);
        assert.equal(calls.length, 0);
        assert.equal(enters, 1);
        assert.equal(leaves, 0);
    });

    test(`implementation argument errors propagate without retry (${order})`, async () => {
        let enters = 0;
        let leaves = 0;
        const { job, calls, filters } = await fixture({
            async enter() { enters++; },
            async leave(_plugin, result) { leaves++; return result; },
        }, companionFirst);
        await assert.rejects(job.annotations.get(-1, false, filters), /Invalid frame/);
        await assert.rejects(job.annotations.get(0, false, null), /Filters must be an array/);
        assert.equal(calls.length, 2);
        assert.equal(enters, 2);
        assert.equal(leaves, 0);
    });

    test(`leave failure propagates after exactly one read (${order})`, async () => {
        const failure = new Error('leave failed');
        let leaves = 0;
        const { job, calls, filters } = await fixture({
            async leave() { leaves++; throw failure; },
        }, companionFirst);
        await assert.rejects(job.annotations.get(0, false, filters), (error) => error === failure);
        assert.equal(calls.length, 1);
        assert.equal(leaves, 1);
    });
}

test('companion leave runs once for each empty-filter or disabled-plugin call', async () => {
    let leaves = 0;
    const { job, filters, disable } = await fixture({
        async leave(_plugin, result) { leaves++; return result; },
    });
    await job.annotations.get(0);
    assert.equal(leaves, 1);
    disable();
    assert.equal((await job.annotations.get(0, false, filters)).length, 1);
    assert.equal(leaves, 2);
});

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

test('overlapping calls keep independent arguments and do not suppress legitimate hooks', async () => {
    const started = deferred();
    const resume = deferred();
    const enters = [];
    const leaves = [];
    const f = await fixture({
        async enter(_plugin, frame) {
            enters.push(frame);
            if (frame === 5) { started.resolve(); await resume.promise; }
        },
        async leave(_plugin, result, frame) { leaves.push(frame); return result; },
    });
    const first = f.job.annotations.get(5, true, f.filters);
    await started.promise;
    // The first call already has its replacement args. Disabling must not alter it.
    f.disable();
    assert.equal((await f.job.annotations.get(6, false, f.filters)).length, 1);
    resume.resolve();
    assert.strictEqual(await first, f.objects);
    assert.deepEqual(enters, [5, 6]);
    assert.deepEqual(leaves, [6, 5]);
    assert.deepEqual(f.calls.map(({ frame, selected }) => [frame, selected.length]), [[6, 1], [5, 0]]);
});

// Exercise the small, generic core option independently of annotation-specific behavior.
async function wrapperFixture(hooks = []) {
    const Registry = registry();
    const calls = [];
    const context = { name: 'caller' };
    async function wrapped(...args) { return Registry.apiWrapper.call(this, wrapped, ...args); }
    wrapped.implementation = function (...args) { calls.push({ context: this, args }); return args; };
    for (const [index, hook] of hooks.entries()) {
        await Registry.register.call({ wrapped }, {
            name: `Hook ${index}`, description: 'Test method arguments', cvat: { wrapped: hook },
        });
    }
    return { call: (...args) => wrapped.apply(context, args), context, calls };
}

test('empty methodArguments means zero method args while hooks see the original args', async () => {
    const original = { value: 1 };
    let seen;
    const f = await wrapperFixture([{
        async enter() { return { methodArguments: [] }; },
        async leave(_plugin, result, arg) { seen = arg; return result; },
    }]);
    assert.deepEqual(await f.call(original), []);
    assert.strictEqual(seen, original);
    assert.strictEqual(f.calls[0].context, f.context);
});

test('the last argument replacement wins and its top-level array is snapshotted', async () => {
    const first = Object.freeze(['first']);
    const second = ['second'];
    const hookArgs = [];
    const f = await wrapperFixture([
        { async enter(_plugin, arg) { hookArgs.push(arg); return { methodArguments: first }; } },
        { async enter(_plugin, arg) { hookArgs.push(arg); return { methodArguments: second }; } },
        { async enter(_plugin, arg) { hookArgs.push(arg); second[0] = 'changed later'; } },
    ]);
    assert.deepEqual(await f.call('original'), ['second']);
    assert.deepEqual(hookArgs, ['original', 'original', 'original']);
    assert.deepEqual(first, ['first']);
});

for (const bad of [null, undefined, {}, 'not an array', 42]) {
    test(`invalid methodArguments is rejected without calling the method: ${String(bad)}`, async () => {
        const f = await wrapperFixture([{ async enter() { return { methodArguments: bad }; } }]);
        await assert.rejects(f.call('original'), /methodArguments must be an array/);
        assert.equal(f.calls.length, 0);
    });
}

test('replacement return keeps precedence over prevention and argument replacement', async () => {
    const f = await wrapperFixture([{
        async enter() {
            return { methodArguments: ['changed'], preventMethodCall: true, preventMethodCallWithReturn: undefined };
        },
    }]);
    assert.strictEqual(await f.call('original'), undefined);
    assert.equal(f.calls.length, 0);
});
