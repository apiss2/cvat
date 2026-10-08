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
    before() {}
    after(node) { this.parentElement.append(node); }
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
        return { drawImage() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {} };
    }
}

function setup() {
    const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
    const get = (id) => { assert(elements.has(id), `missing element: ${id}`); return elements.get(id); };
    const uploads = new Element();
    const weightLabel = new Element('label'); uploads.append(weightLabel); weightLabel.append(get('weights'));
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
    const boot = /\nresetForm\(\);\nrun\(checkSession\);\s*$/;
    assert(boot.test(app), 'unexpected application boot sequence');
    vm.runInContext(app.replace(boot, '\n'), context);
    vm.runInContext(fs.readFileSync(path.join(root, 'static/updates.js'), 'utf8'), context);
    vm.runInContext(`
        let pending;
        run = (handler) => { pending = Promise.resolve().then(handler).catch((error) => notice(error.message, true)); };
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

for (const [kind, outputName] of [['tag', 'Classification'], ['polygon', 'Segmentation'], ['rectangle', 'Detection']]) {
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
    const panel = ui.get('weights').parentElement.parentElement.children.find((node) => node.id === 'retained-model-files');
    panel.children.find((node) => node.tagName === 'label').children[0].files = [file('new-local-name.onnx')];
    await ui.submit('register-form');
    const { route, data } = ui.sandbox.submitted[0];
    assert.equal(route, `/api/models/${'a'.repeat(20)}/update`);
    assert.deepEqual([...data.keys()], ['weights']);
    assert.equal(data.get('weights').name, 'model.onnx');
});

test('metadata-only update does not convert tag labels to polygons', async () => {
    const ui = setup(); ui.sandbox.model = model(); ui.eval('resetForm(model)');
    ui.get('description').value = 'updated';
    await ui.submit('register-form');
    const data = ui.sandbox.submitted[0].data;
    assert.deepEqual([...data.keys()], ['manifest']);
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
