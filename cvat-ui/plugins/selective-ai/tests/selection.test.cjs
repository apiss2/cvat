// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const { selectFrames, searchFrames, validateCondition, taskFilterForID } = require('./load.cjs')('selection.ts');
const job = (start_frame, stop_frame, extra = {}) => ({ id: 1, task_id: 5, type: 'annotation', start_frame, stop_frame, frame_count: stop_frame - start_frame + 1, ...extra });
const filters = [{ '==': [{ var: 'label' }, 'A'] }];

test('ordinary job ranges are relative, unioned, sorted and exclude deleted frames', () => {
    assert.deepEqual(selectFrames({ size: 9, start_frame: 100, frame_filter: 'step=3', deleted_frames: [4] },
        [job(5, 7), job(2, 5)], 5), [2, 3, 5, 6, 7]);
    assert.deepEqual(selectFrames({ size: 9 }, [], 5), []);
});

test('invalid ranges, foreign jobs and unexpected job types fail closed', () => {
    for (const value of [job(-1, 1), job(0, 9), job(4, 2), job(0, 2, { task_id: 6 }),
        job(0, 2, { type: 'replica' }), job(0, 2, { frame_count: 2 })]) assert.throws(() => selectFrames({ size: 9 }, [value], 5));
    assert.throws(() => selectFrames({ size: 2, deleted_frames: [2] }, [], 5));
});

test('empty conditions are valid and task rules are conjoined with the selected ID', () => {
    validateCondition({ taskFilter: {}, jobFilter: {}, filters: [] });
    assert.throws(() => validateCondition({ taskFilter: [], jobFilter: {}, filters: [] }));
    assert.throws(() => validateCondition({ taskFilter: {}, jobFilter: {}, filters: [null] }));
    assert.deepEqual(JSON.parse(taskFilterForID(filters[0], 5)), { and: [{ '==': [{ var: 'id' }, 5] }, filters[0]] });
});

test('image search only visits candidate ranges, preserving object and element rules', async () => {
    const calls = []; let loads = 0;
    const rules = [...filters, { '==': [{ var: 'occluded' }, false] }];
    const session = { size: 12, annotations: {
        async clear(options) { assert.deepEqual(options, { reload: true }); loads++; },
        async search(from, to, options) {
            calls.push([from, to]); assert.deepEqual(options, { allowDeletedFrames: true, annotationsFilters: rules });
            return [1, 3, 5, 8, 10].find((frame) => frame >= from && frame <= to) ?? null;
        },
    } };
    assert.deepEqual(await searchFrames(session, [0, 1, 2, 5, 6, 8, 9], rules, () => {}), [1, 5, 8]);
    assert.deepEqual(calls, [[0, 2], [2, 2], [5, 6], [6, 6], [8, 9], [9, 9]]);
    assert.equal(loads, 1);
    assert.deepEqual(await searchFrames(session, [0, 2], [], () => {}), [0, 2]);
    assert.equal(loads, 1);
});

test('cancellation and out-of-range search results cannot broaden selection', async () => {
    let cancelled = false; let calls = 0;
    const session = { size: 5, annotations: { clear: async () => {}, search: async () => {
        calls++; cancelled = true; return 1;
    } } };
    await assert.rejects(searchFrames(session, [0, 1, 3], filters, () => {
        if (cancelled) throw new Error('cancelled');
    }), /cancelled/);
    assert.equal(calls, 1);
    session.annotations.search = async () => 4;
    await assert.rejects(searchFrames(session, [0, 1], filters, () => {}), /検索結果/);
});
