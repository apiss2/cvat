// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const source = path.resolve(__dirname, '../../../src/components/annotation-page/top-bar/statistics-modal.tsx');
const compiled = ts.transpileModule(fs.readFileSync(source, 'utf8'), {
    fileName: source,
    reportDiagnostics: true,
    compilerOptions: {
        target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.React, esModuleInterop: true,
    },
});
assert.deepEqual((compiled.diagnostics || []).filter((item) => item.category === ts.DiagnosticCategory.Error), []);

const React = { createElement: (type, props, ...children) => ({ type, props, children }) };
function render(sections, dimension, collecting = false) {
    const module = { exports: {} };
    const mocks = {
        react: React,
        'react-redux': { connect: () => (component) => component },
        'antd/lib/grid': { Row: 'Row', Col: 'Col' },
        '@ant-design/icons': { QuestionCircleOutlined: 'QuestionCircleOutlined' },
        'antd/lib/table': 'Table',
        'antd/lib/modal': 'Modal',
        'antd/lib/spin': 'Spin',
        'antd/lib/typography/Text': 'Text',
        'components/common/cvat-tooltip': 'Tooltip',
        'cvat-core-wrapper': { DimensionType: { DIMENSION_2D: '2d' } },
        'actions/annotation-actions': { showStatistics: () => {} },
        'utils/hooks': { usePlugins: () => sections },
    };
    vm.runInNewContext(compiled.outputText, {
        module, exports: module.exports,
        require: (id) => {
            if (id in mocks) return mocks[id];
            throw new Error(`Unexpected import: ${id}`);
        },
    }, { filename: source });
    const counts = Object.fromEntries(
        ['rectangle', 'polygon', 'polyline', 'points', 'ellipse', 'cuboid', 'skeleton', 'mask']
            .map((type) => [type, { shape: 0, track: 0 }]),
    );
    const closeStatistics = () => {};
    return {
        closeStatistics,
        modal: module.exports.default({
            collecting, visible: true, assignee: 'Nobody', startFrame: 0, stopFrame: 1,
            bugTracker: null, closeStatistics, dimension, jobInstance: { id: 1 },
            data: { label: {}, total: { ...counts, tag: 0, manually: 0, interpolated: 0, total: 0 } },
        }),
    };
}

function find(node, predicate) {
    if (Array.isArray(node)) return node.map((child) => find(child, predicate)).find(Boolean);
    if (!node || typeof node !== 'object') return undefined;
    return predicate(node) ? node : find(node.children, predicate);
}

test('the shared Info modal bounds its body and keeps the original statistics table with or without plugins', () => {
    const Section = () => null;
    for (const sections of [[], [{ component: Section, weight: 30 }]]) {
        for (const dimension of ['2d', '3d']) {
            for (const collecting of [false, true]) {
                const { modal, closeStatistics } = render(sections, dimension, collecting);
                assert.equal(modal.type, 'Modal');
                assert.equal(modal.props.onOk, closeStatistics);
                assert.equal(modal.props.styles.content.display, 'flex');
                assert.equal(modal.props.styles.content.flexDirection, 'column');
                assert.equal(modal.props.styles.content.maxHeight, 'calc(100vh - 48px)');
                assert.equal(modal.props.styles.body.minHeight, 0);
                assert.equal(modal.props.styles.body.display, 'flex');
                assert.equal(modal.props.styles.body.flexDirection, 'column');
                assert.equal(modal.props.styles.body.overflow, 'hidden');
                assert.equal(modal.props.styles.footer.flexShrink, 0);
                const table = find(modal, (node) => node.type === 'Table');
                if (collecting) {
                    assert.equal(table, undefined);
                    assert.ok(find(modal, (node) => node.type === 'Spin'));
                } else {
                    assert.equal(table.props.scroll.y, 400);
                    assert.equal(table.props.scroll.x, 'max-content');
                    assert.equal(table.props.size, undefined);
                    assert.equal(table.props.columns.length, dimension === '2d' ? 13 : 3);
                }
                const window = find(modal, (node) => node.props?.className === 'cvat-job-info-modal-window');
                const children = window.children.flat();
                const statistics = children.findIndex((node) => node?.props?.className === 'cvat-job-info-statistics');
                assert.ok(statistics >= 0);
                const section = children.findIndex((node) => node?.type === Section);
                assert.equal(section >= 0, sections.length > 0);
                if (sections.length) assert.ok(section < statistics);
            }
        }
    }
});

test('only a rendered task context panel in Info can compact the following statistics table', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../src/scss/styles.scss'), 'utf8');
    assert.match(css, /\.cvat-job-info-modal-window > \.cvat-task-context ~ \.cvat-job-info-statistics \.ant-table-body\s*\{\s*max-height: 200px !important;\s*\}/);
    assert.equal((css.match(/max-height:\s*200px/g) || []).length, 1);
});
