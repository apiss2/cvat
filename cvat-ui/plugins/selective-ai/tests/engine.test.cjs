// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const engine = require('./load.cjs')('engine.ts');
const { selectFrames, buildPlan, executePlan, checkedOutput, buildMapping, getAll } = engine;
const scope = { type: 'task', id: 5 };
const condition = { mode: 'any', labelIds: [1] };
const model = { id: 'onnx-model-r1', name: 'model', kind: 'detector', labels: [{ name: 'seg', type: 'mask' }] };
const choices = { seg: 'mask' };
const clone = (value) => structuredClone(value);

function fixture() {
    const labels = [{ id: 1, name: 'A', type: 'tag' }, { id: 2, name: 'B', type: 'tag' },
        { id: 10, name: 'mask', type: 'mask' }];
    const task = { id: 5, project_id: 7, name: 'images', size: 4, dimension: '2d' };
    const meta = { size: 4, deleted_frames: [], start_frame: 100, frame_filter: 'step=3',
        frames: Array.from({ length: 4 }, (_, i) => ({ name: `image-${i}.png` })) };
    const data = { tags: [{ frame: 0, label_id: 1 }, { frame: 1, label_id: 2 },
        { frame: 2, label_id: 1 }, { frame: 2, label_id: 2 }],
    shapes: [{ id: 100, frame: 3, label_id: 10, points: [1, 2, 3, 4] }], tracks: [] };
    const calls = []; const writes = []; const gets = []; const active = [];
    const api = {
        async get(path, params = {}) {
            gets.push([path, clone(params)]);
            if (path === '/labels') return { count: labels.length, next: null, results: clone(labels) };
            if (path === '/lambda/requests') return clone(active);
            if (path === '/tasks') return { count: 1, next: null, results: [clone(task)] };
            if (path === '/tasks/5') return clone(task);
            if (path === '/tasks/5/data/meta') return clone(meta);
            if (path === '/tasks/5/annotations') return clone(data);
            throw new Error(`Unexpected GET: ${path}`);
        },
        async infer(taskId, modelId, args) {
            calls.push({ taskId, modelId, args: clone(args) });
            return { tags: [], shapes: [{ frame: args.frame, label_id: 10, source: 'auto',
                type: 'mask', points: [0, 3, 1, 0, 0, 1, 1], attributes: [], group: null }], tracks: [] };
        },
        async append(taskId, output) {
            writes.push({ taskId, data: clone(output) });
            data.shapes.push(...clone(output.shapes)); data.tags.push(...clone(output.tags));
        },
    };
    return { api, labels, task, meta, data, calls, writes, gets, active };
}
async function run(f, opts = {}) {
    const plan = opts.plan ?? await buildPlan(f.api, scope, condition);
    return executePlan(f.api, plan, opts.selected ?? new Set(plan.frames.map((row) => row.key)),
        opts.model ?? model, opts.choices ?? choices, opts.threshold ?? 0.5, false,
        opts.cancelled ?? (() => false), opts.progress ?? (() => {}));
}

test('any/all/none/untagged evaluate tags only, deduplicate tags and skip deleted frames', () => {
    const tags = [{ frame: 0, label_id: 1 }, { frame: 0, label_id: 1 }, { frame: 1, label_id: 2 },
        { frame: 2, label_id: 1 }, { frame: 2, label_id: 2 }];
    const meta = { size: 5, deleted_frames: [4] };
    assert.deepEqual(selectFrames(meta, tags, { mode: 'any', labelIds: [1, 2] }), [0, 1, 2]);
    assert.deepEqual(selectFrames(meta, tags, { mode: 'all', labelIds: [1, 2] }), [2]);
    assert.deepEqual(selectFrames(meta, tags, { mode: 'none', labelIds: [1] }), [1, 3]);
    assert.deepEqual(selectFrames(meta, tags, { mode: 'untagged', labelIds: [] }), [3]);
});

test('selection matches a reference predicate for every combination of three tag labels', () => {
    for (let bits = 0; bits < 8; bits++) {
        const tags = [1, 2, 3].filter((id) => bits & (1 << (id - 1))).map((label_id) => ({ label_id, frame: 0 }));
        for (let chosen = 1; chosen < 8; chosen++) {
            const labelIds = [1, 2, 3].filter((id) => chosen & (1 << (id - 1)));
            const contains = (id) => tags.some((tag) => tag.label_id === id);
            for (const mode of ['any', 'all', 'none']) {
                const expected = mode === 'any' ? labelIds.some(contains) : mode === 'all' ? labelIds.every(contains) : !labelIds.some(contains);
                assert.equal(selectFrames({ size: 1 }, tags, { mode, labelIds }).length, Number(expected));
            }
        }
    }
});

test('invalid conditions or metadata fail closed rather than select every image', () => {
    for (const condition of [{ mode: 'none', labelIds: [] }, { mode: 'invalid', labelIds: [1] },
        { mode: 'any', labelIds: [-1] }, { mode: 'all', labelIds: [1.2] }]) {
        assert.throws(() => selectFrames({ size: 2 }, [], condition));
    }
    assert.throws(() => selectFrames({ size: 2, deleted_frames: [2] }, [], condition));
    assert.throws(() => selectFrames({ size: 2 }, [{ frame: 20, label_id: 1 }], condition));
});

test('preview is read-only and API frame indices are not source start/step indices', async () => {
    const f = fixture(); const plan = await buildPlan(f.api, scope, condition);
    assert.deepEqual(plan.frames.map((item) => item.frame), [0, 2]);
    assert.equal(plan.frames[1].name, 'image-2.png');
    assert.equal(f.calls.length + f.writes.length, 0);
    assert.equal(JSON.stringify(plan).includes('shapes'), false);
    const untagged = await buildPlan(f.api, scope, { mode: 'untagged', labelIds: [] });
    assert.deepEqual(untagged.frames.map((item) => item.frame), [3], 'existing segmentation is not a tag');
});

test('project preview includes all pages of tasks and never follows next URLs', async () => {
    const f = fixture(); const pages = [];
    const api = { ...f.api, async get(path, params) {
        if (path === '/tasks') {
            pages.push(params.page);
            return { count: 101, next: params.page === 1 ? 'https://untrusted.invalid/page2' : null,
                results: Array.from({ length: params.page === 1 ? 100 : 1 }, (_, i) => ({
                    ...f.task, id: (params.page - 1) * 100 + i + 1,
                })) };
        }
        if (/\/data\/meta$/.test(path)) return clone(f.meta);
        if (/\/annotations$/.test(path)) return clone(f.data);
        return f.api.get(path, params);
    } };
    const plan = await buildPlan(api, { type: 'project', id: 7 }, condition);
    assert.deepEqual(pages, [1, 2]); assert.equal(plan.tasks.length, 101); assert.equal(plan.frames.length, 202);
});

test('pagination rejects truncation, duplicates and count changes', async () => {
    const badPages = [
        [{ count: 2, next: null, results: [{ id: 1 }] }],
        [{ count: 2, next: 'x', results: [{ id: 1 }] }, { count: 2, next: null, results: [{ id: 1 }] }],
        [{ count: 2, next: 'x', results: [{ id: 1 }] }, { count: 3, next: null, results: [{ id: 2 }, { id: 3 }] }],
    ];
    for (const pages of badPages) await assert.rejects(getAll({ get: async () => pages.shift() }, '/tasks', {}));
});

test('missing tag labels, foreign project membership and 3D tasks cannot broaden a plan', async () => {
    const f = fixture();
    await assert.rejects(buildPlan(f.api, scope, { mode: 'none', labelIds: [999] }));
    await assert.rejects(buildPlan(f.api, { type: 'project', id: 8 }, condition));
    f.task.dimension = '3d'; await assert.rejects(buildPlan(f.api, scope, condition));
    assert.equal(f.writes.length, 0);
});

test('user deselection restricts both inference and writes; existing annotations survive', async () => {
    const f = fixture(); const old = clone(f.data); const report = await run(f, { selected: new Set(['5:2']) });
    assert.equal(report.error, undefined);
    assert.deepEqual(f.calls.map((call) => call.args.frame), [2]);
    assert.deepEqual(f.writes[0].data.shapes[0].points, [0, 3, 1, 0, 0, 1, 1]);
    assert.deepEqual(f.calls[0].args.mapping, { seg: { name: 'mask', attributes: {} } });
    assert.deepEqual(f.data.tags, old.tags); assert.deepEqual(f.data.shapes[0], old.shapes[0]);
    assert.deepEqual(report.outcomes, [{ taskId: 5, frame: 2, status: 'saved' }]);
});

test('task-start recheck removes ineligible frames without adding newly matching frames', async () => {
    const f = fixture(); const plan = await buildPlan(f.api, scope, condition);
    f.data.tags = [{ frame: 1, label_id: 1 }, { frame: 2, label_id: 1 }];
    const report = await run(f, { plan });
    assert.deepEqual(f.calls.map((call) => call.args.frame), [2]);
    assert.deepEqual(report.outcomes.map((item) => item.status), ['skipped', 'saved']);
});

test('deleted frames and moved tasks are rechecked before inference', async () => {
    const f = fixture(); const plan = await buildPlan(f.api, scope, condition);
    f.meta.deleted_frames = [0, 2]; await run(f, { plan }); assert.equal(f.calls.length, 0);
    const projectPlan = await buildPlan(f.api, { type: 'project', id: 7 }, condition);
    // A plan with candidates must be captured before moving/deleting its task.
    f.meta.deleted_frames = []; const plan2 = await buildPlan(f.api, { type: 'project', id: 7 }, condition);
    f.task.project_id = 8; const report = await run(f, { plan: plan2 });
    assert.ok(report.error); assert.equal(f.calls.length, 0); assert.equal(projectPlan.frames.length, 0);
});

test('normal automatic annotation already queued or running blocks this task', async () => {
    const f = fixture(); f.active.push({ status: 'started', function: { task: 5 } });
    const report = await run(f); assert.match(report.error, /実行中/); assert.equal(f.calls.length, 0);
});

test('classification tags can be saved on untagged frames without clearing masks', async () => {
    const f = fixture(); const oldMask = clone(f.data.shapes[0]);
    const plan = await buildPlan(f.api, scope, { mode: 'untagged', labelIds: [] });
    f.api.infer = async (_, __, args) => ({ tags: [{ frame: args.frame, label_id: 1, source: 'auto' }], shapes: [], tracks: [] });
    const report = await run(f, { plan, model: { ...model, labels: [{ name: 'A', type: 'tag' }] }, choices: { A: 'A' } });
    assert.equal(report.outcomes[0].status, 'saved'); assert.deepEqual(f.data.shapes[0], oldMask);
    assert.equal(f.data.tags.at(-1).frame, 3);
});

test('invalid output frames, IDs, labels and tracks cause no write', async () => {
    for (const patch of [{ frame: 3 }, { id: 7 }, { label_id: 999 }]) {
        const f = fixture(); f.api.infer = async () => ({ tags: [], shapes: [{ frame: 0, label_id: 10, ...patch }], tracks: [] });
        const report = await run(f); assert.ok(report.error); assert.equal(f.writes.length, 0);
    }
    assert.throws(() => checkedOutput({ tags: [], shapes: [], tracks: [{}] }, 0, new Set([10])));
    assert.throws(() => checkedOutput({ tags: [], shapes: [{ frame: 0, label_id: 10,
        elements: [{ frame: 1, label_id: 11 }] }], tracks: [] }, 0, new Set([10])));
});

test('empty detections do not issue writes', async () => {
    const f = fixture(); f.api.infer = async () => ({ tags: [], shapes: [], tracks: [] });
    const report = await run(f); assert.equal(f.writes.length, 0);
    assert.deepEqual(report.outcomes.map((item) => item.status), ['empty', 'empty']);
});

test('cancellation during inference discards the not-yet-saved result', async () => {
    const f = fixture(); let cancel = false; const infer = f.api.infer;
    f.api.infer = async (...args) => { const result = await infer(...args); cancel = true; return result; };
    const report = await run(f, { cancelled: () => cancel });
    assert.equal(report.cancelled, true); assert.equal(f.calls.length, 1); assert.equal(f.writes.length, 0);
});

test('cancellation after one write keeps completed output and prevents the next call', async () => {
    const f = fixture(); let cancel = false;
    const report = await run(f, { cancelled: () => cancel, progress: (r) => { if (r.outcomes.length) cancel = true; } });
    assert.equal(report.cancelled, true); assert.equal(f.calls.length, 1); assert.equal(f.writes.length, 1);
});

test('uncertain save is reported, never retried, and previous successful frames remain', async () => {
    const f = fixture(); const append = f.api.append; let attempted = 0;
    f.api.append = async (...args) => {
        attempted++; await append(...args);
        if (attempted === 2) throw new Error('connection lost after server accepted data');
    };
    const report = await run(f);
    assert.match(report.error, /保存結果が不明/); assert.equal(attempted, 2);
    assert.deepEqual(report.outcomes.map((item) => item.status), ['saved', 'failed']); assert.equal(f.writes.length, 2);
});

test('inference failure stops before later frames and retains prior successes', async () => {
    const f = fixture(); const infer = f.api.infer;
    f.api.infer = async (...args) => { if (args[2].frame === 2) throw new Error('inference unavailable'); return infer(...args); };
    const report = await run(f); assert.match(report.error, /inference unavailable/); assert.equal(f.writes.length, 1);
});

test('invalid selection, threshold, interactor or empty mapping performs no inference', async () => {
    for (const opts of [{ selected: new Set(['5:999']) }, { selected: new Set() }, { threshold: NaN },
        { threshold: -0.1 }, { model: { ...model, kind: 'interactor' } }, { choices: {} }]) {
        const f = fixture(); const report = await run(f, opts); assert.ok(report.error); assert.equal(f.calls.length, 0);
    }
});

test('mapping preserves same-name attributes and recursively maps skeleton children', () => {
    const model = [{ name: 'body', type: 'skeleton', sublabels: [{ name: 'point', type: 'points', attributes: [{ name: 'visible' }] }] }];
    const labels = [{ name: 'person', type: 'skeleton', sublabels: [{ name: 'point', type: 'points', attributes: [{ name: 'visible' }] }] }];
    assert.deepEqual(buildMapping(model, labels, { body: 'person' }), { body: { name: 'person', attributes: {},
        sublabels: { point: { name: 'point', attributes: { visible: 'visible' } } } } });
    assert.throws(() => buildMapping([{ name: 'mask', type: 'mask' }], [{ name: 'tag', type: 'tag' }], { mask: 'tag' }));
});
