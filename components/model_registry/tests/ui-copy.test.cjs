// SPDX-License-Identifier: MIT
// UI copy and DOM contract checks without a live CVAT server or browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function fixture() {
    const nodes = new Map();
    const handlers = new Map();
    const element = () => ({
        textContent: '', value: '', hidden: false, disabled: false, children: [], files: [],
        classList: { toggle() {} }, setAttribute() {}, reset() {},
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        addEventListener() {},
    });
    for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
        assert(!nodes.has(match[1]), `duplicate HTML id: ${match[1]}`);
        const node = element();
        node.addEventListener = (event, handler) => handlers.set(`${match[1]}:${event}`, handler);
        nodes.set(match[1], node);
    }
    const context = vm.createContext({
        URL, console, setTimeout, setInterval() {},
        location: { href: 'https://cvat.example/model-registry/', hash: '#models' },
        window: { addEventListener() {} },
        document: {
            hidden: false, addEventListener() {}, createElement: element,
            getElementById(id) { assert(nodes.has(id), `missing HTML id: ${id}`); return nodes.get(id); },
        },
    });
    // Suppress only the two startup calls; exercise the production functions below.
    const startup = /resetForm\(\);\nrun\(checkSession\);\s*$/;
    assert(startup.test(source));
    vm.runInContext(source.replace(startup, ''), context);
    return { nodes, handlers, context, run: (code) => vm.runInContext(code, context) };
}

test('registration guidance describes the inputs, not implementation classes', () => {
    assert.doesNotMatch(html, /ModelBase|loadとpredict|manifest\.json|Polygon変換/);
    assert.match(html, /model\.py/);
    assert.match(html, /更新の検証に失敗しても、公開中の版は変わりません/);
    assert.match(html, /ZIP内の設定を使用し、上のフォームへの入力は使用しません/);
    assert.match(html, /領域内の穴は塗りつぶされます/);
    assert.match(html, /連絡先はログインした利用者全員に表示されます/);
});

test('each contour setting has accessible, actionable help', () => {
    const { nodes } = fixture();
    for (const id of ['min-distance', 'spacing-percent', 'min-area']) {
        assert(nodes.has(`${id}-help`));
        assert.match(html, new RegExp(`<input id="${id}"[^>]*aria-describedby="${id}-help"`));
    }
    assert.match(html, /細かな凹凸を省略/);
    assert.match(html, /小さな対象も残す場合/);
});

test('diagnostics remain available but are collapsed by default', () => {
    assert.match(html, /<details><summary>詳細な推論結果<\/summary><pre id="test-result"><\/pre><\/details>/);
    assert.match(html, /<details class="panel form-panel"><summary>処理ログ<\/summary>/);
    assert.match(html, /id="download-logs"/);
    assert.match(source, /JSON\.stringify\(\{ request_id: result\.request_id, revision: result\.revision/);
});

test('model kind retains its values and gives the correct registration instructions', () => {
    const { nodes, run } = fixture();
    nodes.get('model-kind').value = 'polygon';
    run('kindChanged()');
    assert.match(nodes.get('label-help').textContent, /出力する順に上から登録/);
    assert.doesNotMatch(nodes.get('label-help').textContent, /N,H,W|model\.py|返却値/);
    assert.equal(nodes.get('polygon-options').hidden, false);
    assert.equal(nodes.get('min-area').disabled, false);
    nodes.get('model-kind').value = 'rectangle';
    run('kindChanged()');
    assert.match(nodes.get('label-help').textContent, /クラスIDとラベル名/);
    assert.equal(nodes.get('polygon-options').hidden, true);
    assert.equal(nodes.get('min-area').disabled, true);
});

test('both empty and nonempty inference results have a readable summary and retain diagnostics', async () => {
    for (const results of [[], [{ type: 'rectangle', points: [1, 2, 3, 4] }]]) {
        const { nodes, handlers, context, run } = fixture();
        context.reply = { results, request_id: 'request-1', revision: 'revision-1' };
        run(`selected = { id: 'model-1', can_manage: true, active_revision: 'revision-1' };
            api = async () => reply; base64File = async () => 'image';
            preview = async () => {}; refreshLogs = async () => {};`);
        handlers.get('test-form:submit')({ preventDefault() {} });
        await new Promise(setImmediate);
        assert.match(nodes.get('test-summary').textContent, results.length ? /1件検出/ : /検出されませんでした/);
        assert.equal(JSON.parse(nodes.get('test-result').textContent).objects, results.length);
        assert.equal(nodes.get('test-button').disabled, false);
    }
});

test('account changes clear the inference summary and diagnostic text', () => {
    const { nodes, run } = fixture();
    nodes.get('test-summary').textContent = 'old account result';
    nodes.get('test-result').textContent = 'old account diagnostic';
    run('resetForm = () => {}; signedOut()');
    assert.equal(nodes.get('test-summary').textContent, '');
    assert.equal(nodes.get('test-result').textContent, '');
});
