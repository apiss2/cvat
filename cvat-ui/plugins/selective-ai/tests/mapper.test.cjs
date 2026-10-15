// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const load = require('./load.cjs');
const { supportsThreshold, thresholdParameters } = load('model-options.ts');

function fixture() {
    let cursor = 0;
    const memo = [];
    const createElement = (type, props, ...children) => ({ type, props: { ...props, children } });
    const useMemo = (factory, deps) => {
        const slot = cursor++;
        if (!memo[slot] || deps.some((dep, i) => dep !== memo[slot].deps[i])) {
            memo[slot] = { deps, value: factory() };
        }
        return memo[slot].value;
    };
    const Mapper = () => {};
    const module = load('mapper.tsx', {
        '@modules/react': { default: { createElement, Fragment: 'fragment' }, useMemo, useCallback: (fn) => fn },
        'components/model-runner-modal/labels-mapper': { default: Mapper },
        'components/model-runner-modal/styles.scss': {},
    });
    return { ...module, Mapper, render: (props) => { cursor = 0; return module.MappingEditor(props); } };
}

test('adapter preserves label and attribute metadata without mutating API data', () => {
    const f = fixture();
    const labels = [{ id: 1, name: 'body', type: 'skeleton', color: '#123456',
        attributes: [{ name: 'visible', input_type: 'checkbox', values: ['false'] }],
        sublabels: [{ name: 'nose', type: 'points' }] }];
    const original = structuredClone(labels);
    const converted = f.toMapperLabels(labels);
    assert.deepEqual(converted[0].attributes, labels[0].attributes);
    assert.equal(converted[0].sublabels[0].name, 'nose');
    converted[0].attributes[0].values.push('true');
    assert.deepEqual(labels, original);
});

test('FullMapping serialization retains explicit attribute and skeleton-point choices', () => {
    const f = fixture();
    const tuple = [[{ name: 'body', type: 'skeleton' }, { name: 'person', type: 'skeleton' },
        [[{ name: 'visible' }, { name: 'reviewed' }], [null, { name: 'unused' }]],
        [[{ name: 'head', type: 'points' }, { name: 'nose', type: 'points' },
            [[{ name: 'score' }, { name: 'confidence' }]], []]]]];
    assert.deepEqual(f.serializeMapping(tuple), { body: { name: 'person', attributes: { visible: 'reviewed' },
        sublabels: { head: { name: 'nose', attributes: { score: 'confidence' } } } } });
    assert.deepEqual(f.serializeMapping([]), {}, 'do not auto-map a deliberately empty selection');
});

test('editor uses the upstream LabelsMapper with stable arrays across unrelated renders', () => {
    const f = fixture(); let getters = 0; const updates = [];
    const model = { id: 'detector', get labels() { getters++; return [{ name: 'seg', type: 'mask' }]; } };
    const labels = [{ id: 10, name: 'mask', type: 'mask' }];
    const props = { model, labels, disabled: false, onChange: (value) => updates.push(value) };
    const editor = (element) => element.props.children[0].props.children[0];
    const first = editor(f.render(props)); const second = editor(f.render(props));
    assert.strictEqual(first.type, f.Mapper);
    assert.strictEqual(first.props.modelLabels, second.props.modelLabels);
    assert.strictEqual(first.props.taskLabels, second.props.taskLabels);
    assert.equal(getters, 1);
    second.props.onUpdateMapping([[first.props.modelLabels[0], first.props.taskLabels[0], [], []]]);
    assert.deepEqual(updates, [{ seg: { name: 'mask', attributes: {} } }]);
});

test('disabled editor remains mounted but hidden and ignores mapping changes', () => {
    const f = fixture(); let updated = 0;
    const view = f.render({ model: { id: 'detector', labels: [] }, labels: [], disabled: true,
        onChange: () => { updated++; } });
    const wrapper = view.props.children[0];
    assert.equal(wrapper.props.hidden, true);
    const child = wrapper.props.children[0]; assert.strictEqual(child.type, f.Mapper);
    child.props.onUpdateMapping([]); assert.equal(updated, 0);
});

test('threshold capability follows the reserved registry ID namespace, not display names', () => {
    assert.equal(supportsThreshold({ id: 'mr-0123456789abcdef0123-0123456789abcdef' }), false);
    assert.equal(supportsThreshold({ id: 'onnx-yolo', name: 'mr-model' }), true);
    assert.equal(supportsThreshold({ id: 5 }), true);
    assert.deepEqual(thresholdParameters({ id: 'mr-reserved' }, null), {});
    assert.deepEqual(thresholdParameters({ id: 'onnx-yolo' }, 0.25), { threshold: 0.25 });
    assert.throws(() => thresholdParameters({ id: 'onnx-yolo' }, null));
});
