// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const test = require('node:test');
const { load, tick } = require('./helpers.cjs');
const { DICOM_FORMAT } = load('messages.ts');

function fixture({ formatFailure } = {}) {
    let mounted; let stateIndex = 0; let effectRun = false;
    const values = []; const calls = []; const accepted = []; const lifecycle = [];
    const React = {
        createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
        useRef: () => ({ current: null }),
        useEffect: (callback) => { if (!effectRun) { effectRun = true; callback(); } },
        useState: (initial) => {
            const index = stateIndex++;
            if (!(index in values)) values[index] = initial;
            return [values[index], (value) => { values[index] = value; }];
        },
    };
    const antd = Object.fromEntries(['Alert', 'Button', 'Modal', 'Select', 'Space'].map((name) => [name, name]));
    antd.Form = Object.assign(() => {}, { Item: 'Form.Item' });
    antd.Radio = Object.assign(() => {}, { Group: 'Radio.Group' });
    antd.Typography = { Text: 'Text', Paragraph: 'Paragraph' };
    const dialog = load('dialog.tsx', {
        react: React,
        'react-dom/client': { createRoot: () => ({ render: (node) => { mounted = node; }, unmount: () => lifecycle.push('unmount') }) },
        '@ant-design/icons': {}, antd,
    }, { document: {
        createElement: () => ({ remove: () => lifecycle.push('remove'), querySelector: () => null }),
        body: { appendChild: () => {} },
    } });
    const handle = dialog.openDialog({
        formats: async () => {
            if (formatFailure) throw formatFailure;
            return [DICOM_FORMAT, 'CVAT 1.1'];
        },
        start: async (...args) => { calls.push(args); return 'accepted-request'; },
        dispose: () => lifecycle.push('abort'),
    }, 'Project', () => lifecycle.push('closed'), (id) => accepted.push(id));
    const render = () => {
        stateIndex = 0;
        const tree = mounted.type(); const nodes = [];
        function visit(node) {
            if (Array.isArray(node)) node.forEach(visit);
            else if (node && typeof node === 'object') { nodes.push(node); visit(node.children); }
        }
        visit(tree); visit(tree.props.footer);
        return { tree, nodes };
    };
    render();
    return { handle, render, values, calls, accepted, lifecycle };
}

test('replace explicitly warns that annotations on absent images are removed', async () => {
    const current = fixture(); await tick();
    let view = current.render();
    assert.equal(view.nodes.find((node) => node.type === 'Select').props.value, 'CVAT 1.1');
    assert.equal(view.nodes.find((node) => node.type === 'Radio.Group').props.value, 'append');
    view.nodes.find((node) => node.type === 'Radio.Group').props.onChange({ target: { value: 'replace' } });
    view = current.render();
    const warning = view.nodes.find((node) => node.type === 'Alert' && node.props.type === 'warning');
    assert.match(warning.props.message, /対象Task.*全体/);
    assert.match(warning.props.description, /入力に含まれない画像.*削除/);
    assert.match(warning.props.description, /DicomTag.*Attribute.*保持/);
});

test('switching to DICOM describes whole-video tags and duplicate rejection', async () => {
    const current = fixture(); await tick();
    current.render().nodes.find((node) => node.type === 'Select').props.onChange(DICOM_FORMAT);
    const { nodes } = current.render();
    assert.match(nodes.find((node) => node.type === 'Alert' && node.props.type === 'info').props.description, /同じフレーム.*同じラベル.*失敗/);
    const description = nodes.find((node) => node.type === 'Paragraph').children.join('');
    assert.match(description, /DicomTag.*指定できません/);
    assert.match(description, /動画では全フレーム/);
});

test('invalid file cannot be selected and valid ZIP preserves explicit mode and format', async () => {
    const current = fixture(); await tick();
    const picker = current.render().nodes.find((node) => node.type === 'input');
    picker.props.onChange({ target: { files: [new File(['{}'], 'sample.json')], value: 'sample.json' } });
    let view = current.render();
    assert.equal(view.nodes.find((node) => node.type === 'Button' && node.props.type === 'primary').props.disabled, true);
    assert.match(view.nodes.find((node) => node.type === 'Alert' && node.props.type === 'error').props.message, /ZIP/);
    const file = new File(['zip'], 'sample.zip');
    picker.props.onChange({ target: { files: [file], value: 'sample.zip' } });
    view = current.render();
    view.nodes.find((node) => node.type === 'Radio.Group').props.onChange({ target: { value: 'replace' } });
    view.nodes.find((node) => node.type === 'Select').props.onChange(DICOM_FORMAT);
    view = current.render();
    const button = view.nodes.find((node) => node.type === 'Button' && node.props.type === 'primary');
    await Promise.all([button.props.onClick(), button.props.onClick()]);
    await tick();
    assert.equal(current.calls.length, 1);
    assert.deepEqual(current.calls[0], [file, DICOM_FORMAT, 'replace']);
    assert.deepEqual(current.accepted, ['accepted-request']);
    view = current.render();
    assert.equal(view.nodes.find((node) => node.type === 'Button' && node.props.type === 'primary').props.disabled, true);
    assert.match(view.nodes.find((node) => node.type === 'Alert' && node.props.type === 'success').props.description, /Requests/);
    current.handle.dispose(); current.handle.dispose();
    assert.deepEqual(current.lifecycle, ['abort', 'unmount', 'remove', 'closed']);
});

test('format loading failure remains visible after selecting a ZIP and stops the spinner', async () => {
    const current = fixture({ formatFailure: new Error('Server unavailable') });
    assert.equal(current.render().nodes.find((node) => node.type === 'Select').props.loading, true);
    await tick();
    let view = current.render();
    const message = view.nodes.find((node) => node.type === 'Alert' && node.props.type === 'error').props.message;
    assert.equal(view.nodes.find((node) => node.type === 'Select').props.loading, false);
    view.nodes.find((node) => node.type === 'input').props.onChange({
        target: { files: [new File(['zip'], 'sample.zip')], value: 'sample.zip' },
    });
    view = current.render();
    assert.equal(view.nodes.find((node) => node.type === 'Alert' && node.props.type === 'error').props.message, message);
    assert.equal(view.nodes.find((node) => node.type === 'Select').props.loading, false);
    assert.equal(view.nodes.find((node) => node.type === 'Button' && node.props.type === 'primary').props.disabled, true);
});
