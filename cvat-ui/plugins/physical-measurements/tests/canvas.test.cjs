// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');

const canvasSource = path.resolve(__dirname, '../../../../cvat-canvas/src/typescript');

function load(file, dependencies = {}, allowUnusedHandlers = false) {
    const module = { exports: {} };
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file,
        reportDiagnostics: true,
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    assert.deepEqual(output.diagnostics, [], `Cannot transpile ${file}`);
    const factory = vm.runInThisContext(`(function(module, exports, require) {${output.outputText}\n})`, { filename: file });
    factory(module, module.exports, (name) => {
        if (name in dependencies) return dependencies[name];
        // The view constructor and drawing handlers require a browser. These tests
        // exercise its real text/update methods with only the SVG surface stubbed.
        if (allowUnusedHandlers) return {};
        throw new Error(`Unexpected dependency: ${name}`);
    });
    return module.exports;
}

const constants = load(path.join(canvasSource, 'consts.ts'));
const modelModule = load(path.join(canvasSource, 'canvasModel.ts'), {
    './consts': constants,
    './master': load(path.join(canvasSource, 'master.ts')),
});
const { CanvasModelImpl } = modelModule;
const { CanvasViewImpl } = load(path.join(canvasSource, 'canvasView.ts'), {
    './consts': constants,
    './canvasModel': modelModule,
    './shared': { stringifyPoints: (points) => points.join(',') },
}, true);
const measurement = load(path.join(__dirname, '../src/ts/measurement.ts'));
const { shapeText } = load(path.join(__dirname, '../src/ts/display.ts'), { './measurement': measurement });

function svgNode(text = '') {
    return {
        text, spans: [], classes: [], attributes: {}, styles: {}, removed: false,
        tspan(value) { const span = svgNode(value); this.spans.push(span); return span; },
        attr(attributes) { Object.assign(this.attributes, attributes); return this; },
        style(styles) { Object.assign(this.styles, styles); return this; },
        addClass(name) { this.classes.push(name); return this; },
        move() { return this; },
        clear() { return this; },
        remove() { this.removed = true; },
    };
}

function fixture() {
    const model = new CanvasModelImpl();
    const state = {
        clientID: 1, shapeType: 'polyline', points: [0, 0, 10, 0, 10, 10], frame: 100,
        label: { id: 1, name: 'Length', attributes: [] },
        source: 'manual', attributes: {}, descriptions: [], group: { id: 0, color: '#fff' },
        rotation: 0, zOrder: 0, hidden: false, outside: false, occluded: false,
    };
    let currentState = state;
    const view = Object.create(CanvasViewImpl.prototype);
    Object.assign(view, {
        controller: { get geometry() { return model.geometry; }, get objects() { return [currentState]; } },
        configuration: model.configuration,
        svgTexts: {}, svgShapes: { 1: svgNode() }, drawnStates: {},
        activeElement: { clientID: null, attributeID: null }, selectedObjects: [],
        background: { style: {}, classList: { add() {}, remove() {} } },
        adoptedText: { text(render) { const text = svgNode(); render(text); return text; } },
        activate() {}, deactivate() {}, transformCanvas() {}, updateTextPosition() {},
        isInnerHidden() { return false; },
        translateToCanvas(points) { return points.slice(); },
    });
    for (const handler of ['edit', 'draw', 'masks', 'autoborder', 'interaction', 'slice']) {
        view[`${handler}Handler`] = { configure() {} };
    }
    view.drawnStates[1] = view.saveState(state);
    model.subscribe(view);
    return {
        model, view, state,
        update(next) { currentState = next; view.updateObjects([next]); },
        extra() { return view.svgTexts[1]?.spans.find((span) => span.classes.includes('cvat_canvas_text_extra'))?.text; },
    };
}

const calibrated = () => ({
    version: 1, default: { x: 0.3, y: 0.4, unit: 'mm' },
    frames: { 101: { x: 0.6, y: 0.8, unit: 'mm' }, 102: null },
    revision: 1, supported: true, unavailable_reason: null, frame_count: 200,
});

test('without a callback the native canvas emits no additional measurement text', () => {
    const f = fixture();
    assert.equal(f.model.configuration.shapeText, null);
    const text = f.view.addText(f.state);
    assert.equal(f.extra(), undefined);
    assert.ok(text.spans[0].text.includes('Length'));
});

test('the callback receives original image coordinates and the current task frame', () => {
    const f = fixture();
    let received;
    f.model.configure({ shapeText: (shape) => { received = shape; return '7 mm'; } });
    f.model.geometry = { ...f.model.geometry, scale: 5, angle: 90, left: 120, top: -40 };
    f.view.addText(f.state);
    assert.deepEqual(received, { shapeType: 'polyline', points: f.state.points, frame: 100 });
    assert.equal(f.extra(), '7 mm');
    assert.deepEqual(f.state.points, [0, 0, 10, 0, 10, 10]);
});

test('calibration replacement and hook removal recreate existing labels immediately', () => {
    const f = fixture();
    f.view.addText(f.state);
    const originalText = f.view.svgTexts[1];
    f.model.configure({ shapeText: shapeText(calibrated()) });
    assert.equal(originalText.removed, true);
    assert.equal(f.extra(), '7 mm');
    const physicalText = f.view.svgTexts[1];
    f.model.configure({ shapeText: null });
    assert.equal(physicalText.removed, true);
    assert.equal(f.extra(), undefined);
    assert.equal(f.model.configuration.shapeText, null);
});

test('stationary tracks recalculate for frame overrides and return to pixels on excluded frames', () => {
    const f = fixture();
    f.model.configure({ shapeText: shapeText(calibrated()) });
    f.view.addText(f.state);
    assert.equal(f.extra(), '7 mm');
    f.update({ ...f.state, frame: 101 });
    assert.equal(f.extra(), '14 mm');
    f.update({ ...f.state, frame: 102 });
    assert.equal(f.extra(), '20 px');
    f.update({ ...f.state, frame: 100 });
    assert.equal(f.extra(), '7 mm');
});

test('vertex changes recalculate from geometry while uncalibrated frame changes retain native text', () => {
    const f = fixture();
    f.model.configure({ shapeText: shapeText(calibrated()) });
    f.view.addText(f.state);
    f.update({ ...f.state, points: [0, 0, 10, 10] });
    assert.equal(f.extra(), '5 mm');
    f.update(f.state);
    assert.equal(f.extra(), '7 mm');
    f.model.configure({ shapeText: null });
    const nativeText = f.view.svgTexts[1];
    f.update({ ...f.state, frame: 101 });
    assert.strictEqual(f.view.svgTexts[1], nativeText);
    assert.equal(nativeText.removed, false);
});
