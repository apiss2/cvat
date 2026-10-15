// SPDX-License-Identifier: MIT
// Copy and DOM contracts. These do not replace a CVAT build or browser test.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const root = path.resolve(__dirname, '../../..');

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
    const startup = /resetForm\(\);\nrun\(checkSession\);\s*$/;
    assert(startup.test(source));
    vm.runInContext(source.replace(startup, ''), context);
    vm.runInContext('let pending; run = (handler) => { pending = Promise.resolve().then(handler); };', context);
    return { nodes, handlers, context, run: (code) => vm.runInContext(code, context) };
}

test('model kinds retain standard ML terms and their existing values', () => {
    for (const [value, name] of [['polygon', 'Segmentation'], ['rectangle', 'Detection'], ['tag', 'Classification']]) {
        assert.match(html, new RegExp(`<option value="${value}">${name}（`));
    }
    assert.doesNotMatch(html, /領域分割|物体検出|ModelBase|loadとpredict/);
    assert.match(html, /ポリゴン変換/);
    assert.match(html, /model\.py/);
    assert.doesNotMatch(html, /zip-form|submit-zip|static\/updates\.js/);
    assert.match(html, /id="retained-model-files"/);
    assert.match(html, /公開中の版を維持/);
    assert.match(html, /領域内の穴は塗りつぶされます/);
    assert.match(html, /連絡先はログインした利用者全員に表示されます/);
});

test('contour settings have accessible guidance without changing their bounds', () => {
    const { nodes } = fixture();
    for (const id of ['min-distance', 'spacing-percent', 'min-area']) {
        assert(nodes.has(`${id}-help`));
        assert.match(html, new RegExp(`<input id="${id}"[^>]*aria-describedby="${id}-help"`));
    }
    for (const match of html.matchAll(/\baria-describedby="([^"]+)"/g)) {
        for (const id of match[1].split(/\s+/)) assert(nodes.has(id), `missing help: ${id}`);
    }
    assert.match(html, /細かい凹凸を省略/);
    assert.match(html, /小さい対象を残す場合/);
    assert.match(html, /id="spacing-percent"[^>]*min="0" max="100"/);
});

test('JSON and logs remain available in closed details elements', () => {
    assert.match(html, /<details><summary>推論結果の詳細（JSON）<\/summary><pre id="test-result"><\/pre><\/details>/);
    assert.match(html, /<details><summary>処理ログを表示<\/summary><pre id="logs"><\/pre><\/details>/);
    assert.match(html, /id="download-logs"/);
    assert.match(source, /JSON\.stringify\(\{ request_id: result\.request_id, revision: result\.revision/);
});

for (const [kind, name] of [['polygon', 'Segmentation'], ['rectangle', 'Detection'], ['tag', 'Classification']]) {
    test(`${name} guidance describes label input rather than SDK internals`, () => {
        const { nodes, run } = fixture();
        nodes.get('model-kind').value = kind;
        run('kindChanged()');
        const help = nodes.get('label-help').textContent;
        assert.match(help, new RegExp(name));
        assert.doesNotMatch(help, /N,H,W|class_id|ModelBase|返却値|空配列/);
        assert.match(help, kind === 'polygon' ? /出力順/ : /クラスID/);
        if (kind === 'tag') assert.match(help, /ROIを指定しない/);
        assert.equal(nodes.get('polygon-options').hidden, kind !== 'polygon');
        assert.equal(nodes.get('min-area').disabled, kind !== 'polygon');
    });
}

for (const [shapes, tags] of [[0, 0], [1, 0], [0, 2], [1, 1]]) {
    test(`summary distinguishes ${shapes} shapes and ${tags} tags, retaining raw results`, async () => {
        const { nodes, handlers, context, run } = fixture();
        const results = [
            ...Array.from({ length: shapes }, () => ({ type: 'rectangle', points: [1, 2, 3, 4] })),
            ...Array.from({ length: tags }, () => ({ type: 'tag', label: '<tag>', confidence: 0.9 })),
        ];
        context.reply = { results, request_id: 'request-1', revision: 'revision-1' };
        run(`selected = { id: 'model-1', can_manage: true, active_revision: 'revision-1', manifest: { labels: [{ type: 'tag' }] } };
            api = async () => reply; base64File = async () => 'image';
            preview = async () => {}; refreshLogs = async () => {};`);
        handlers.get('test-form:submit')({ preventDefault() {} });
        await run('pending');
        assert.match(nodes.get('test-summary').textContent, new RegExp(`図形 ${shapes}件、タグ ${tags}件`));
        assert.equal(JSON.parse(nodes.get('test-result').textContent).objects, results.length);
        assert.equal(nodes.get('test-button').disabled, false);
        if (tags) assert.match(nodes.get('test-tags').textContent, /信頼度 0\.900/);
    });
}

test('account and model changes clear both human-readable results and diagnostics', async () => {
    const { nodes, run } = fixture();
    const ids = ['test-summary', 'test-tags', 'test-result'];
    for (const id of ids) nodes.get(id).textContent = 'old result';
    run('resetForm = () => {}; signedOut()');
    for (const id of ids) assert.equal(nodes.get(id).textContent, '');
    for (const id of ids) nodes.get(id).textContent = 'old result';
    run("user = { id: 1 }; api = async () => ({ id: 'next', owner: 'owner', manifest: { labels: [] }, can_manage: false });");
    await run("selectModel('next')");
    for (const id of ids) assert.equal(nodes.get(id).textContent, '');
});

test('selective annotation describes classification and the specific threshold limitation', () => {
    const dialog = fs.readFileSync(path.join(root, 'cvat-ui/plugins/selective-ai/src/ts/dialog.tsx'), 'utf8');
    assert.match(dialog, /Segmentation、Detection、Classification/);
    assert.match(dialog, /この画面から信頼度の閾値を変更できません/);
    assert.doesNotMatch(dialog, /検出条件は変更できません|検出なし|現在の通信/);
    assert.match(dialog, /empty: '出力なし'/);
    assert.match(dialog, /再実行すると重複する場合があります/);
    assert.match(dialog, /保存した結果は残ります/);
    assert.match(dialog, /各フレームを1枚の画像/);
});

test('tracking guidance covers masks and reverse direction without losing warnings', () => {
    const dialog = fs.readFileSync(path.join(root, 'cvat-ui/plugins/sam2/src/ts/tracking-dialog.tsx'), 'utf8');
    assert.match(dialog, /ポリゴンとマスクを追跡/);
    assert.match(dialog, /順方向（後のフレーム）/);
    assert.match(dialog, /逆方向（前のフレーム）/);
    assert.match(dialog, /分離した領域は保持されません/);
    assert.match(dialog, /既存トラックは変更しません/);
    assert.match(dialog, /結果を確認して保存してください/);
    assert.doesNotMatch(dialog, /メモリ量と結果サイズ|初期ポリゴン/);
});
