// SPDX-License-Identifier: MIT
// Exercise the real registration/update handlers with a small DOM fixture.
// This is not a browser/CVAT-server integration test.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');

class Element {
    constructor(tag = 'div') {
        this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = [];
        this.hidden = false; this.disabled = false; this.required = false;
        this.value = ''; this.files = []; this.text = '';
        this.classList = { toggle() {} };
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map((node) => node.textContent).join(''); }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    replaceChildren(...nodes) { this.children = []; this.text = ''; this.append(...nodes); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(type, handler, capture = false) { this.listeners.push({ type, handler, capture }); }
    querySelectorAll(selector) {
        const matches = [];
        const key = /^\[data-key=(\w+)\]$/.exec(selector)?.[1];
        for (const node of this.children) {
            if (node.dataset.key === key) matches.push(node);
            matches.push(...node.querySelectorAll(selector));
        }
        return matches;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0]; }
    reset() {}
    getContext() {
        return { drawImage() {}, strokeRect() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {} };
    }
}

function setup() {
    const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
    const get = (id) => { assert(elements.has(id), `missing element: ${id}`); return elements.get(id); };
    const uploads = new Element();
    const weightLabel = new Element('label'); uploads.append(weightLabel); weightLabel.append(get('weights'));
    uploads.append(get('retained-model-files'));
    get('register-form').reset = () => { for (const id of ['code', 'weights', 'sample']) get(id).files = []; };
    const sandbox = {
        document: { getElementById: get, createElement: (tag) => new Element(tag), addEventListener() {}, hidden: false },
        window: { addEventListener() {} }, location: { href: 'https://cvat.invalid/model-registry/', hash: '#models' },
        URL, FormData, File, Uint8Array, console, setInterval() {}, setTimeout, clearTimeout,
        btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
        createImageBitmap: async () => ({ width: 24, height: 20, close() {} }),
        submitted: [], response: null,
    };
    const context = vm.createContext(sandbox);
    const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
    const boot = /\r?\nresetForm\(\);\r?\nrun\(checkSession\);\s*$/;
    assert(boot.test(app), 'unexpected application boot sequence');
    vm.runInContext(app.replace(boot, '\n'), context);
    vm.runInContext(`
        let pending;
        run = (handler) => { pending = Promise.resolve().then(handler).catch((error) => notice(error.message, true)); };
        globalThis.actualSendPackage = sendPackage;
        sendPackage = async (route, data) => submitted.push({ route, data });
        api = async (route) => route.endsWith('/logs') ? [] : response;
        user = { id: 1, name: 'alice', admin: false };
        resetForm();
    `, context);
    return {
        get, sandbox, context,
        eval: (code) => vm.runInContext(code, context),
        async submit(id) {
            let stopped = false;
            const event = { preventDefault() {}, stopImmediatePropagation() { stopped = true; } };
            for (const entry of [...get(id).listeners].filter((x) => x.type === 'submit').sort((a, b) => Number(b.capture) - Number(a.capture))) {
                entry.handler(event);
                if (stopped) break;
            }
            await vm.runInContext('pending', context);
        },
    };
}

function model(kind = 'tag') {
    const manifest = JSON.parse(fs.readFileSync(path.join(root, 'examples/classification/manifest.json'), 'utf8'));
    manifest.labels.forEach((label) => { label.type = kind; });
    return { id: 'a'.repeat(20), active_revision: 'b'.repeat(16), owner: 'alice', can_manage: true,
        manifest, revisions: [{ revision: 'b'.repeat(16), manifest }] };
}
function file(name) { return new File(['fixture'], name); }

for (const [kind, outputName] of [['tag', 'Classification'], ['polygon', 'Segmentation'], ['rectangle', 'Detection'], ['polyline', 'Line detection']]) {
    test(`new registration retains ${kind} label types and correct options`, async () => {
        const ui = setup(); ui.get('model-kind').value = kind; ui.eval('kindChanged()');
        assert.match(ui.get('label-help').textContent, new RegExp(outputName));
        assert.equal(ui.get('polygon-options').hidden, kind !== 'polygon');
        assert.equal(ui.get('min-area').disabled, kind !== 'polygon');
        ui.get('name').value = 'test'; ui.get('weights').files = [file('model.onnx')];
        ui.get('code').files = [file('model.py')]; ui.get('sample').files = [file('sample.png')];
        await ui.submit('register-form');
        const { route, data } = ui.sandbox.submitted[0];
        assert.equal(route, '/api/upload');
        assert.equal(data.has('model_id'), false);
        assert.equal(data.has('expected_revision'), false);
        const manifest = JSON.parse(data.get('manifest'));
        assert.equal(manifest.labels[0].type, kind);
        assert.equal('polygon' in manifest, kind === 'polygon');
    });
}

test('HTML exposes classification and accessible tag result output', () => {
    assert.match(html, /<option value="tag">Classification（画像タグ出力）<\/option>/);
    assert.match(html, /id="test-tags" role="status"/);
});

test('classification update restores type and needs no new code or sample', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    assert.equal(ui.get('model-kind').value, 'tag');
    assert.equal(ui.get('code').required, false); assert.equal(ui.get('sample').required, false);
    const panel = ui.get('retained-model-files');
    panel.children.find((node) => node.tagName === 'label').children[0].files = [file('model.onnx')];
    await ui.submit('register-form');
    const { route, data } = ui.sandbox.submitted[0];
    assert.equal(route, `/api/models/${'a'.repeat(20)}/update`);
    assert.deepEqual([...data.keys()], ['weights', 'expected_revision']);
    assert.equal(data.get('expected_revision'), 'b'.repeat(16));
    assert.equal(data.has('model_id'), false);
    assert.equal(data.get('weights').name, 'model.onnx');
});

for (const name of ['new-local-name.onnx', 'Model.onnx']) {
    test(`weight name ${name} is rejected without uploading`, async () => {
        const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
        const panel = ui.get('retained-model-files');
        panel.children.find((node) => node.tagName === 'label').children[0].files = [file(name)];
        await ui.submit('register-form');
        assert.equal(ui.sandbox.submitted.length, 0);
        assert.match(ui.get('notice').textContent, /ONNXファイル名はmodel\.onnxと一致/);
    });
}

test('code filename mismatch is rejected without uploading', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('code').files = [file('renamed.py')];
    await ui.submit('register-form');
    assert.equal(ui.sandbox.submitted.length, 0);
    assert.match(ui.get('notice').textContent, /ファイル名はmodel\.py/);
});

test('code-only update retains the model.py filename and other files', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('code').files = [file('model.py')];
    await ui.submit('register-form');
    const { route, data } = ui.sandbox.submitted[0];
    assert.equal(route, `/api/models/${'a'.repeat(20)}/update`);
    assert.deepEqual([...data.keys()], ['code', 'expected_revision']);
    assert.equal(data.get('expected_revision'), 'b'.repeat(16));
    assert.equal(data.has('model_id'), false);
    assert.equal(data.get('code').name, 'model.py');
});

test('registration exposes individual files without a ZIP entry', () => {
    assert.doesNotMatch(html, /zip-form|submit-zip|accept="\.zip"/);
});

function promptingModel() {
    const manifest = JSON.parse(fs.readFileSync(path.join(root, 'examples/prompting/manifest.json'), 'utf8'));
    return { ...model('polygon'), manifest, revisions: [{ revision: 'b'.repeat(16), manifest }] };
}

test('prompting registration sends capabilities and explicit sample prompts', async () => {
    const ui = setup(); ui.get('model-kind').value = 'interactor'; ui.eval('kindChanged()');
    ui.get('name').value = 'prompt model'; ui.get('weights').files = [file('model.onnx')];
    ui.get('sample-prompts').value = '{"pos_points":[[12,8]],"neg_points":[[1,1]],"obj_bbox":null}';
    assert.equal(ui.get('polygon-options').hidden, true);
    assert.equal(ui.get('interactor-options').hidden, false);
    assert.equal(ui.get('sample-prompts').disabled, false);
    await ui.submit('register-form');
    const manifest = JSON.parse(ui.sandbox.submitted[0].data.get('manifest'));
    assert.equal(manifest.kind, 'interactor');
    assert.equal(manifest.labels[0].type, 'polygon');
    assert.deepEqual(manifest.interactor.sample.pos_points, [[12, 8]]);
    assert.equal(manifest.interactor.startswith_box, false);
    assert.equal(manifest.interactor.startswith_box_optional, true);
});

test('prompting update retains files and sample, changing only requested settings', async () => {
    const ui = setup(); ui.sandbox.model = promptingModel(); ui.eval('resetForm(model)');
    assert.equal(ui.get('model-kind').value, 'interactor');
    assert.equal(ui.get('code').required, false);
    assert.deepEqual(JSON.parse(ui.get('sample-prompts').value), ui.sandbox.model.manifest.interactor.sample);
    ui.get('description').value = 'changed';
    await ui.submit('register-form');
    assert.deepEqual(JSON.parse(ui.sandbox.submitted[0].data.get('manifest')), { description: 'changed' });
    assert.deepEqual([...ui.sandbox.submitted[0].data.keys()], ['manifest', 'expected_revision']);
});

test('prompting can disable both the box and negative points on an existing revision', async () => {
    const ui = setup();
    ui.sandbox.model = promptingModel();
    ui.eval('resetForm(model)');
    ui.get('box-mode').value = 'disabled';
    ui.get('min-neg-points').value = '-1';
    await ui.submit('register-form');
    const patch = JSON.parse(ui.sandbox.submitted[0].data.get('manifest'));
    assert.equal(patch.interactor.startswith_box, false);
    assert.equal(patch.interactor.startswith_box_optional, false);
    assert.equal(patch.interactor.min_neg_points, -1);
});

test('switching away from prompting explicitly clears its capabilities', async () => {
    const ui = setup(); ui.sandbox.model = promptingModel(); ui.eval('resetForm(model)');
    ui.get('model-kind').value = 'rectangle'; ui.eval('kindChanged()');
    await ui.submit('register-form');
    const patch = JSON.parse(ui.sandbox.submitted[0].data.get('manifest'));
    assert.equal(patch.kind, 'detector'); assert.equal(patch.interactor, null);
    assert.equal(patch.labels[0].type, 'rectangle');
    assert.equal(ui.get('sample-prompts').disabled, true);
});

test('invalid prompting JSON and multiple labels are rejected before upload', async () => {
    const ui = setup(); ui.get('model-kind').value = 'interactor';
    for (const json of ['invalid', '{"text_prompts":["object"]}', '[]']) {
        ui.get('sample-prompts').value = json;
        await ui.submit('register-form');
        assert.equal(ui.sandbox.submitted.length, 0);
        assert.notEqual(ui.get('notice').textContent, '');
    }
    ui.eval("labelRow({ id: 1, name: 'extra' })");
    await ui.submit('register-form');
    assert.match(ui.get('notice').textContent, /1件だけ/);
});

test('prompting list and revision-aware test display masks and send prompts', async () => {
    const ui = setup(); const requestBodies = [];
    ui.sandbox.model = promptingModel();
    ui.sandbox.requestBodies = requestBodies;
    ui.eval(`selected = model; models = [model]; renderModels();
        api = async (route, options) => {
            if (route.endsWith('/logs')) return [];
            requestBodies.push(JSON.parse(options.body)); return response;
        };`);
    assert.equal(ui.get('models').children[0].children[1].textContent, 'Prompting');
    ui.get('revision').value = ui.sandbox.model.active_revision; ui.eval('testRevisionChanged()');
    assert.equal(ui.get('test-prompts-field').hidden, false);
    ui.get('test-image').files = [file('sample.png')];
    ui.get('test-prompts').value = '{"pos_points":[],"neg_points":[],"obj_bbox":[[0,0],[24,20]]}';
    ui.sandbox.response = { revision: 'b'.repeat(16), request_id: 'test', results: { mask: [[0, 1], [1, 0]] } };
    await ui.submit('test-form');
    assert.equal(ui.get('notice').textContent, '');
    assert.equal(ui.get('preview').hidden, false);
    assert.match(ui.get('test-summary').textContent, /出力マスク: 1件/);
    const payload = JSON.parse(JSON.stringify(requestBodies[0]));
    assert.deepEqual(payload.obj_bbox, [[0, 0], [24, 20]]);
    assert.deepEqual(payload.pos_points, []);
    ui.sandbox.response.results = { shapes: [] };
    await ui.submit('test-form');
    assert.match(ui.get('test-summary').textContent, /出力マスク: 0件/);
    ui.sandbox.model.revisions.push({ revision: 'old-detector', manifest: model().manifest });
    ui.get('revision').value = 'old-detector'; ui.eval('testRevisionChanged()');
    assert.equal(ui.get('test-prompts-field').hidden, true);
    assert.equal(ui.get('test-prompts').disabled, true);
    ui.sandbox.response.results = [];
    await ui.submit('test-form');
    assert.deepEqual(Object.keys(requestBodies[2]), ['image']);
});

test('changing revision during image reading keeps the submitted prompts with their revision', async () => {
    const ui = setup();
    const original = promptingModel();
    const next = structuredClone(original.manifest);
    next.interactor.sample.pos_points = [[20, 12]];
    original.revisions.push({ revision: 'c'.repeat(16), manifest: next });
    ui.sandbox.model = original;
    ui.sandbox.requests = [];
    ui.eval(`selected = model;
        api = async (route, options) => {
            if (route.endsWith('/logs')) return [];
            requests.push({ route, body: options.body });
            return { revision: model.active_revision, results: { mask: [[1]] } };
        };`);
    ui.get('revision').value = original.active_revision;
    ui.eval('testRevisionChanged()');
    let startReading;
    let finishReading;
    const reading = new Promise((resolve) => { startReading = resolve; });
    const contents = new Promise((resolve) => { finishReading = resolve; });
    const sample = file('sample.png');
    sample.arrayBuffer = () => { startReading(); return contents; };
    ui.get('test-image').files = [sample];

    const submission = ui.submit('test-form');
    await reading;
    ui.get('revision').value = 'c'.repeat(16);
    ui.eval('testRevisionChanged()');
    finishReading(new Uint8Array([1]).buffer);
    await submission;

    const [request] = ui.sandbox.requests;
    assert.equal(request.route, `/api/models/${original.id}/test?revision=${original.active_revision}`);
    assert.deepEqual(JSON.parse(request.body).pos_points, original.manifest.interactor.sample.pos_points);
    assert.equal(ui.get('test-summary').textContent, '');
    assert.equal(ui.get('preview').hidden, true);
});

test('line detection registers polyline labels and previews any number of lines', async () => {
    const ui = setup(); ui.get('model-kind').value = 'polyline'; ui.eval('kindChanged()');
    assert.equal(ui.get('polygon-options').hidden, true);
    assert.equal(ui.get('interactor-options').hidden, true);
    assert.match(ui.get('label-help').textContent, /任意の本数/);
    ui.get('name').value = 'line model'; ui.get('code').files = [file('model.py')];
    ui.get('weights').files = [file('model.onnx')]; ui.get('sample').files = [file('sample.png')];
    await ui.submit('register-form');
    const manifest = JSON.parse(ui.sandbox.submitted[0].data.get('manifest'));
    assert.equal(manifest.labels[0].type, 'polyline');
    assert.equal(manifest.kind, undefined); assert.equal(manifest.polygon, undefined);

    ui.sandbox.model = model('polyline'); ui.eval('resetForm(model)');
    assert.equal(ui.get('model-kind').value, 'polyline');
    ui.eval('selected = model; models = [model]; renderModels();');
    assert.equal(ui.get('models').children[0].children[1].textContent, 'Line detection');
    ui.get('revision').value = ui.sandbox.model.active_revision; ui.eval('testRevisionChanged()');
    ui.get('test-image').files = [file('sample.png')];
    const line = { label: 'dark_image', confidence: 0.9, type: 'polyline' };
    ui.sandbox.response = { revision: 'b'.repeat(16), request_id: 'test', results: [
        { ...line, points: [0, 0, 10, 10] }, { ...line, points: [1, 1, 5, 1, 5, 5] }, { ...line, points: [2, 2, 3, 3] },
    ] };
    await ui.submit('test-form');
    assert.equal(ui.get('notice').textContent, '');
    assert.equal(ui.get('preview').hidden, false);
    assert.match(ui.get('test-summary').textContent, /出力件数: 3件（図形 3件、タグ 0件）/);
});

test('metadata-only update does not convert tag labels to polygons', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('description').value = 'updated';
    await ui.submit('register-form');
    const data = ui.sandbox.submitted[0].data;
    assert.deepEqual([...data.keys()], ['manifest', 'expected_revision']);
    assert.equal(data.get('expected_revision'), 'b'.repeat(16));
    assert.equal(data.has('model_id'), false);
    assert.deepEqual(JSON.parse(data.get('manifest')), { description: 'updated' });
});

test('unchanged classification update is rejected instead of rewriting label types', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    await ui.submit('register-form');
    assert.equal(ui.sandbox.submitted.length, 0);
    assert.match(ui.get('notice').textContent, /変更するファイルまたは設定/);
});

test('model list distinguishes classification from spatial models', () => {
    const ui = setup(); ui.sandbox.entries = ['tag', 'rectangle', 'polygon'].map((kind) => model(kind));
    ui.eval('models = entries; renderModels()');
    assert.deepEqual(ui.get('models').children.map((row) => row.children[1].textContent), ['Classification', 'Detection', 'Segmentation']);
});

for (const count of [0, 1, 2]) {
    test(`test inference displays ${count} classification tags without geometry`, async () => {
        const ui = setup(); ui.sandbox.model = model(); ui.eval('selected = model');
        ui.get('revision').value = ui.sandbox.model.active_revision; ui.get('test-image').files = [file('sample.png')];
        ui.sandbox.response = { revision: ui.sandbox.model.active_revision, request_id: 'test',
            results: [{ type: 'tag', label: '<img src=x onerror=alert(1)>', confidence: .9 }, { type: 'tag', label: 'second', confidence: .8 }].slice(0, count) };
        await ui.submit('test-form');
        assert.equal(ui.get('notice').textContent, '');
        assert.equal(ui.get('preview').hidden, false);
        if (count) assert.match(ui.get('test-tags').textContent, /信頼度 0\.900/);
        else assert.equal(ui.get('test-tags').textContent, '分類タグ: なし');
        if (count === 2) assert.match(ui.get('test-tags').textContent, /second/);
        assert.equal(ui.get('test-tags').children.length, 0); // Text, not model-supplied HTML.
        ui.eval('signedOut()');
        assert.equal(ui.get('test-tags').textContent, '');
    });
}

test('unpublished models cannot enter the partial-update form', async () => {
    const ui = setup();
    ui.sandbox.response = { ...model(), active_revision: null, revisions: [] };
    await ui.eval('selectModel(response.id)');
    assert.equal(ui.get('update-model').disabled, true);
    assert.equal(ui.get('delete-model').disabled, false);
});

test('deleted model details allow retrying failed cleanup without enabling updates', async () => {
    const ui = setup();
    ui.sandbox.response = { ...model(), deleted: true };
    await ui.eval('selectModel(response.id)');
    assert.equal(ui.get('delete-model').disabled, false);
    assert.equal(ui.get('update-model').disabled, true);
});

test('common upload transport never injects model or revision fields', async () => {
    const ui = setup(); const sent = [];
    ui.sandbox.XMLHttpRequest = class {
        constructor() { this.upload = {}; this.status = 202; this.responseText = '{"id":"operation"}'; }
        open() {}
        setRequestHeader() {}
        getResponseHeader() { return null; }
        send(data) { sent.push(data); this.onload(); }
    };
    ui.sandbox.model = model();
    ui.sandbox.data = new FormData(); ui.sandbox.data.set('manifest', '{}');
    ui.eval(`editing = model; refresh = async () => {}; resetForm = () => {};
        api = async () => ({ status: 'succeeded', model_id: model.id });`);
    await ui.eval("actualSendPackage('/api/upload', data)");
    assert.equal(sent.length, 1);
    assert.deepEqual([...sent[0].keys()], ['manifest']);
});

test('registration and updates use one non-capturing submit handler', async () => {
    const ui = setup();
    const listeners = ui.get('register-form').listeners.filter((entry) => entry.type === 'submit');
    assert.equal(listeners.length, 1);
    assert.equal(listeners[0].capture, false);
    assert.doesNotMatch(html, /static\/updates\.js/);
    const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
    assert.doesNotMatch(source, /stopImmediatePropagation|\bresetForm\s*=/);
    let observed = 0;
    ui.get('register-form').addEventListener('submit', () => { observed++; });
    ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('description').value = 'updated';
    await ui.submit('register-form');
    assert.equal(observed, 1);
    assert.equal(ui.sandbox.submitted.length, 1);
});

test('sample-only update sends only the replacement and base revision', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('sample').files = [file('new-sample.jpg')];
    await ui.submit('register-form');
    assert.equal(ui.sandbox.submitted.length, 1);
    const { route, data } = ui.sandbox.submitted[0];
    assert.equal(route, `/api/models/${ui.sandbox.model.id}/update`);
    assert.deepEqual([...data.keys()], ['sample', 'expected_revision']);
    assert.equal(data.get('sample').name, 'new-sample.jpg');
});

test('multiple weight replacements preserve filenames and omit retained files', async () => {
    const ui = setup(); ui.sandbox.model = model();
    ui.sandbox.model.manifest.weights = ['encoder.onnx', 'decoder.onnx', 'retained.onnx'];
    ui.eval('resetForm(model)');
    const inputs = ui.get('retained-model-files').children.filter((node) => node.tagName === 'label').map((node) => node.children[0]);
    inputs[0].files = [file('encoder.onnx')]; inputs[1].files = [file('decoder.onnx')];
    await ui.submit('register-form');
    const { data } = ui.sandbox.submitted[0];
    assert.deepEqual(data.getAll('weights').map((weight) => weight.name), ['encoder.onnx', 'decoder.onnx']);
    assert.equal(data.has('manifest'), false);
    assert.equal(data.has('code'), false); assert.equal(data.has('sample'), false);
});

for (const firstKind of ['tag', 'polygon', 'rectangle']) {
    test(`metadata-only update preserves mixed labels starting with ${firstKind}`, async () => {
        const ui = setup(); ui.sandbox.model = model(firstKind);
        ui.sandbox.model.manifest.labels[1].type = firstKind === 'tag' ? 'rectangle' : 'tag';
        ui.eval('resetForm(model)'); ui.get('description').value = '';
        await ui.submit('register-form');
        assert.deepEqual(JSON.parse(ui.sandbox.submitted[0].data.get('manifest')), { description: '' });
    });
}

test('intentional model kind change updates all label types', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('model-kind').value = 'rectangle'; ui.eval('kindChanged()');
    await ui.submit('register-form');
    const patch = JSON.parse(ui.sandbox.submitted[0].data.get('manifest'));
    assert.deepEqual(Object.keys(patch), ['labels']);
    assert(patch.labels.every((label) => label.type === 'rectangle'));
    assert(ui.sandbox.model.manifest.labels.every((label) => label.type === 'tag'));
});

test('polygon option change retains the other model settings', async () => {
    const ui = setup(); ui.sandbox.model = model('polygon'); ui.eval('resetForm(model)');
    ui.get('min-area').value = '25';
    await ui.submit('register-form');
    assert.deepEqual(JSON.parse(ui.sandbox.submitted[0].data.get('manifest')),
        { polygon: { min_distance_px: 2, spacing_percent: 1, min_area_px: 25 } });
});

for (const reset of ['resetForm()', 'signedOut()']) {
    test(`${reset} clears update controls and restores required registration files`, async () => {
        const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
        const input = ui.get('retained-model-files').children.find((node) => node.tagName === 'label').children[0];
        input.files = [file('model.onnx')]; ui.get('code').files = [file('model.py')];
        ui.eval('updateSummary()'); assert.match(ui.get('update-summary').textContent, /model\.onnx/);
        ui.eval(reset);
        assert.equal(ui.get('retained-model-files').hidden, true);
        assert.equal(ui.get('retained-model-files').children.length, 0);
        assert.equal(ui.get('update-summary').textContent, '');
        assert.equal(ui.get('weights').parentElement.hidden, false);
        for (const id of ['code', 'weights', 'sample']) {
            assert.equal(ui.get(id).required, true); assert.equal(ui.get(id).files.length, 0);
        }
        assert.equal(ui.get('submit-model').textContent, '検証して公開');
        ui.get('name').value = 'new model'; ui.get('weights').files = [file('new.onnx')];
        ui.get('code').files = [file('model.py')]; ui.get('sample').files = [file('sample.png')];
        await ui.submit('register-form');
        const { route, data } = ui.sandbox.submitted[0];
        assert.equal(route, '/api/upload'); assert.equal(data.has('expected_revision'), false);
        assert.deepEqual(data.getAll('weights').map((weight) => weight.name), ['new.onnx']);
    });
}

test('switching models discards old replacements and uses the new revision', async () => {
    const ui = setup(); ui.sandbox.first = model(); ui.sandbox.second = model();
    ui.sandbox.second.id = 'c'.repeat(20); ui.sandbox.second.active_revision = 'd'.repeat(16);
    ui.sandbox.second.manifest.weights = ['second.onnx'];
    ui.eval('resetForm(first)');
    ui.get('retained-model-files').children.find((node) => node.tagName === 'label').children[0].files = [file('model.onnx')];
    ui.eval('resetForm(second)'); ui.get('description').value = 'second update';
    await ui.submit('register-form');
    const { route, data } = ui.sandbox.submitted[0];
    assert.equal(route, `/api/models/${ui.sandbox.second.id}/update`);
    assert.equal(data.get('expected_revision'), ui.sandbox.second.active_revision);
    assert.equal(data.has('weights'), false);
});

test('replacement summary follows code, sample and weight input changes', () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    const weight = ui.get('retained-model-files').children.find((node) => node.tagName === 'label').children[0];
    for (const [input, name] of [[ui.get('code'), 'model.py'], [ui.get('sample'), 'sample.png'], [weight, 'model.onnx']]) {
        input.files = [file(name)];
        for (const entry of input.listeners.filter((listener) => listener.type === 'change')) entry.handler();
    }
    const summary = ui.get('update-summary').textContent;
    for (const value of ['model.py', '試験画像', 'model.onnx', ui.sandbox.model.active_revision]) assert(summary.includes(value));
});

test('failed validation keeps the update form and its base revision', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('description').value = 'retry me';
    ui.sandbox.XMLHttpRequest = class {
        constructor() { this.upload = {}; this.status = 202; this.responseText = '{"id":"operation"}'; }
        open() {} setRequestHeader() {} getResponseHeader() { return null; }
        send() { this.onload(); }
    };
    ui.eval(`sendPackage = actualSendPackage; refresh = async () => {};
        api = async () => ({ status: 'failed', model_id: model.id, detail: 'invalid output' });`);
    await ui.submit('register-form');
    assert.equal(ui.get('description').value, 'retry me');
    assert.equal(ui.get('retained-model-files').hidden, false);
    assert.equal(ui.eval('editing.active_revision'), ui.sandbox.model.active_revision);
    assert.equal(ui.get('submit-model').disabled, false);
    assert.match(ui.get('notice').textContent, /検証に失敗/);
});

test('upload response from a signed-out account is rejected', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.sandbox.XMLHttpRequest = class {
        constructor() { this.upload = {}; this.status = 202; this.responseText = '{"id":"operation"}'; }
        open() {} setRequestHeader() {} getResponseHeader() { return null; }
        send() { ui.eval('signedOut()'); this.onload(); }
    };
    ui.sandbox.data = new FormData();
    await assert.rejects(ui.eval("actualSendPackage('/api/upload', data)"), (error) => error.constructor.name === 'StaleSession');
    assert.equal(ui.eval('activeOperation'), '');
    assert.equal(ui.get('retained-model-files').hidden, true);
});
