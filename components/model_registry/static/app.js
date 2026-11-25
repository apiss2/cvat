/* SPDX-License-Identifier: MIT */
'use strict';
const $ = (id) => document.getElementById(id);
const base = new URL('./', location.href);
let user = null, authEpoch = 0, checkingSession = false;
class StaleSession extends Error {}
class AccessUnavailable extends Error {}
let selected = null, editing = null, models = [], activeOperation = '', refreshingEpoch = null, detailSequence = 0;
let replacementWeights = [];
const endpoint = (path) => new URL(path.replace(/^\//, ''), base).href;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function authHeaders() { return { 'X-Registry-Request': '1', ...(user ? { 'X-Registry-User-ID': String(user.id) } : {}) }; }
function sameAccount(left, right) { return Boolean(left && right && left.id === right.id && left.name === right.name && left.admin === right.admin); }
function signedOut() {
    authEpoch++; user = null; selected = null; editing = null; models = []; activeOperation = ''; detailSequence++;
    $('workspace').hidden = true; $('operation').hidden = true; $('access-panel').hidden = false; $('account').hidden = true; $('identity').textContent = ''; $('avatar').textContent = '';
    $('models').replaceChildren();
    for (const id of ['logs', 'test-result', 'test-summary', 'test-tags', 'operation', 'detail-title', 'detail-info', 'detail-contact', 'detail-description', 'detail-labels', 'detail-polygon', 'catalog-count']) $(id).textContent = '';
    $('search').value = ''; $('revision').replaceChildren(); $('preview').hidden = true; $('preview').width = 0; $('preview').height = 0; $('test-form').reset();
    $('submit-model').disabled = false; $('upload-progress').hidden = true; resetForm();
}
function accessError(status) {
    signedOut(); notice('');
    const messages = {
        401: ['CVATのセッションを確認できません', 'CVATへ戻ってアカウントの状態を確認し、モデル管理を開き直してください。'],
        403: ['CVATのアカウントを利用できません', '管理者にアカウントと権限の確認を依頼してください。'],
        429: ['CVATへのアクセスが一時的に制限されています', 'しばらく待ってから再試行してください。'],
        502: ['CVATとの連携に失敗しました', 'CVATに接続できません。再試行しても改善しない場合は、管理者に接続設定の確認を依頼してください。'],
    };
    const [title, help] = messages[status] || ['CVATとの接続を確認できません', '時間をおいて再試行してください。改善しない場合は管理者に接続設定の確認を依頼してください。'];
    $('access-title').textContent = title; $('access-help').textContent = help; $('access-help').hidden = false;
    $('loading-indicator').hidden = true; $('access-panel').setAttribute('aria-busy', 'false'); $('access-actions').hidden = false;
}
async function api(path, options = {}) {
    const epoch = authEpoch;
    const response = await fetch(endpoint(path), { ...options, credentials: 'same-origin', cache: 'no-store', headers: { ...authHeaders(), ...(options.headers || {}) } });
    if (epoch !== authEpoch) throw new StaleSession();
    if (response.headers.get('X-Registry-Account-Changed') === '1') { await recoverAccount(); throw new StaleSession(); }
    if (!response.ok) {
        let message; try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail); } catch { message = `HTTP ${response.status}`; }
        if (epoch !== authEpoch) throw new StaleSession();
        if (response.status === 401 || (path === '/api/me' && [403, 429, 502, 503].includes(response.status))) {
            accessError(response.status); throw new AccessUnavailable();
        }
        throw new Error(message);
    }
    const result = await response.json();
    if (epoch !== authEpoch) throw new StaleSession();
    return result;
}
async function run(handler) { try { await handler(); } catch (error) { if (!(error instanceof StaleSession) && !(error instanceof AccessUnavailable)) notice(error.message, true); } }
function button(text, handler) { const b = document.createElement('button'); b.textContent = text; b.type = 'button'; b.addEventListener('click', () => run(handler)); return b; }
function fieldError(id, message) {
    const element = $(id); element.textContent = message; element.hidden = !message;
}
function invalidField(id, message) {
    fieldError(id, message);
    throw new Error(message);
}
function negativePointsChanged() {
    $('min-neg-points').disabled = $('model-kind').value !== 'interactor' || $('disable-neg-points').checked;
}
function labelControls() {
    const count = $('labels').children.length;
    $('add-label').disabled = $('model-kind').value === 'interactor' && count >= 1;
}
function kindChanged() {
    const prompting = $('model-kind').value === 'interactor';
    const polygonOutput = $('model-kind').value === 'polygon' || prompting;
    $('polygon-options').hidden = !polygonOutput;
    $('interactor-options').hidden = !prompting; $('sample-prompts-field').hidden = !prompting;
    for (const id of ['min-pos-points', 'min-neg-points', 'box-mode', 'sample-prompts']) $(id).disabled = !prompting;
    for (const id of ['min-distance', 'max-error', 'min-area']) $(id).disabled = !polygonOutput;
    fieldError('label-help', '');
    $('disable-neg-points').disabled = !prompting;
    negativePointsChanged();
    labelControls();
}
function labelRow(value = { id: 0, name: '' }) {
    const row = document.createElement('div'); row.className = 'label-row';
    for (const [key, title, type] of [['id', 'クラスID', 'number'], ['name', 'ラベル名', 'text']]) {
        const label = document.createElement('label'); label.textContent = title; const input = document.createElement('input');
        input.dataset.key = key; input.type = type; input.required = true;
        if (type === 'number') { input.min = '0'; input.max = String(2 ** 31 - 1); input.step = '1'; } else input.maxLength = 128;
        input.value = value[key]; label.append(input); row.append(label);
    }
    row.append(button('削除', () => { if ($('labels').children.length <= 1) throw new Error('ラベルは1件以上必要です。'); row.remove(); labelControls(); fieldError('label-help', ''); })); $('labels').append(row); labelControls();
}
function resetForm(model = null) {
    editing = model; $('register-form').reset(); $('labels').replaceChildren();
    for (const id of ['min-distance-help', 'max-error-help', 'min-area-help', 'min-pos-error', 'sample-prompts-help', 'label-help', 'code-error']) fieldError(id, '');
    $('form-title').textContent = model ? `モデルの更新: ${model.manifest?.name || model.id}` : 'モデルの新規登録';
    $('name').value = model?.manifest?.name || ''; $('description').value = model?.manifest?.description || ''; $('author-contact').value = model?.manifest?.author_contact || '';
    const labelType = model?.manifest?.labels?.[0]?.type;
    $('model-kind').value = model?.manifest?.kind === 'interactor' ? 'interactor' : ['rectangle', 'polyline', 'tag'].includes(labelType) ? labelType : 'polygon';
    const interactor = model?.manifest?.interactor;
    $('min-pos-points').value = interactor?.min_pos_points ?? 1; $('min-neg-points').value = Math.max(0, interactor?.min_neg_points ?? 0); $('disable-neg-points').checked = interactor?.min_neg_points === -1;
    $('box-mode').value = interactor?.startswith_box ? 'required' : interactor?.startswith_box_optional === false ? 'disabled' : 'optional';
    $('sample-prompts').value = JSON.stringify(interactor?.sample || { pos_points: [[0, 0]], neg_points: [], obj_bbox: null }, null, 2);
    const polygon = model?.manifest?.polygon || {}; $('min-distance').value = polygon.min_distance_px ?? 2; $('max-error').value = polygon.max_error_px ?? 1; $('min-area').value = polygon.min_area_px ?? 10;
    (model?.manifest?.labels || [{ id: 0, name: 'object' }]).forEach(labelRow); $('cancel-update').hidden = !model; kindChanged();
    const updating = Boolean(model?.active_revision);
    const panel = $('retained-model-files');
    panel.hidden = !updating; panel.replaceChildren(); replacementWeights = [];
    $('weights').parentElement.hidden = updating;
    for (const id of ['code', 'weights', 'sample']) $(id).required = !updating;
    $('submit-model').textContent = updating ? '変更を検証して更新' : '検証して公開';
    if (updating) {
        for (const name of model.manifest.weights) {
            const label = document.createElement('label');
            label.className = 'file-field'; label.textContent = `ONNX: ${name}`;
            const input = document.createElement('input');
            input.type = 'file'; input.accept = '.onnx';
            input.setAttribute('aria-label', `${name}と差し替えるONNXファイル`);
            input.addEventListener('change', updateSummary);
            const retained = document.createElement('span'); retained.className = 'help';
            const error = document.createElement('span'); error.className = 'field-error'; error.setAttribute('role', 'alert'); error.hidden = true;
            label.append(input, retained, error); panel.append(label); replacementWeights.push({ name, input, retained, error });
        }
    }
    updateSummary();
}
function updateSummary() {
    const summary = $('update-summary');
    const updating = Boolean(editing?.active_revision);
    summary.textContent = updating ? `更新元の版: ${editing.active_revision}` : '';
    for (const id of ['code', 'sample']) {
        const status = $(`${id}-retained`);
        status.textContent = updating && !$(id).files.length ? '現行を使用' : '';
        status.hidden = !status.textContent;
    }
    fieldError('code-error', '');
    for (const { input, retained, error } of replacementWeights) {
        retained.textContent = input.files.length ? '' : '現行を使用';
        retained.hidden = !retained.textContent;
        error.textContent = ''; error.hidden = true;
    }
}
function readModelSettings(previous) {
    const kind = $('model-kind').value;
    const labelKind = kind === 'interactor' ? 'polygon' : kind;
    const labels = [...$('labels').children].map((row) => {
        const id = Number(row.querySelector('[data-key=id]').value);
        const previousLabel = previous?.labels.find((label) => label.id === id);
        // Keep mixed label types unless the user changes the model kind.
        return { id, name: row.querySelector('[data-key=name]').value.trim(),
            type: labelKind === previous?.labels[0]?.type ? (previousLabel?.type || labelKind) : labelKind };
    });
    const settings = { name: $('name').value.trim(), description: $('description').value,
        author_contact: $('author-contact').value.trim(), labels };
    if (kind === 'polygon' || kind === 'interactor') {
        settings.polygon = {};
        for (const [id, key] of [['min-distance', 'min_distance_px'], ['max-error', 'max_error_px'], ['min-area', 'min_area_px']]) {
            const value = Number($(id).value);
            fieldError(`${id}-help`, '');
            if ($(id).value === '' || !Number.isFinite(value) || value < 0) invalidField(`${id}-help`, '0以上の数値を指定してください。');
            settings.polygon[key] = value;
        }
    }
    if (kind === 'interactor') {
        fieldError('label-help', '');
        if (labels.length !== 1) invalidField('label-help', 'Promptingの説明用ラベルは1件だけにしてください。');
        fieldError('min-pos-error', '');
        const minPos = Number($('min-pos-points').value);
        const minNeg = $('disable-neg-points').checked ? -1 : Number($('min-neg-points').value);
        const boxMode = $('box-mode').value;
        if (boxMode !== 'required' && minPos === 0) invalidField('min-pos-error', '正の点を0にする場合は矩形Promptを必須にしてください。');
        const sample = readPrompts('sample-prompts');
        if (boxMode === 'required' && !sample.obj_bbox) invalidField('sample-prompts-help', '矩形Promptを指定してください。');
        if (boxMode === 'disabled' && sample.obj_bbox) invalidField('sample-prompts-help', '矩形Promptは使用できません。');
        if (sample.pos_points.length < minPos) invalidField('sample-prompts-help', '正の点が最小数に達していません。矩形だけで推論する場合は正の点の最小数を0にしてください。');
        if (minNeg === -1 && sample.neg_points.length) invalidField('sample-prompts-help', '除外点を使用しない設定です。');
        if (sample.neg_points.length < Math.max(0, minNeg)) invalidField('sample-prompts-help', '除外点が最小数に達していません。');
        settings.kind = 'interactor';
        settings.interactor = { min_pos_points: minPos,
            min_neg_points: minNeg, startswith_box: boxMode === 'required',
            startswith_box_optional: boxMode === 'optional', sample };
    } else if (previous?.kind) {
        settings.kind = 'detector';
        if (previous.interactor) settings.interactor = null;
    }
    return settings;
}
function readPrompts(id) {
    const errorId = id === 'sample-prompts' ? 'sample-prompts-help' : 'test-prompts-error';
    fieldError(errorId, '');
    let prompts;
    try { prompts = JSON.parse($(id).value); }
    catch { invalidField(errorId, '有効なJSONを入力してください。'); }
    if (!prompts || Array.isArray(prompts) || typeof prompts !== 'object' ||
        Object.keys(prompts).some((key) => !['pos_points', 'neg_points', 'obj_bbox'].includes(key)) ||
        !Array.isArray(prompts.pos_points) || !Array.isArray(prompts.neg_points)) {
        invalidField(errorId, 'PromptのJSONにはpos_pointsとneg_pointsの配列、任意のobj_bboxだけを指定してください。');
    }
    const point = (p) => Array.isArray(p) && p.length === 2 && p.every((n) => typeof n === 'number' && Number.isFinite(n) && n >= 0);
    for (const key of ['pos_points', 'neg_points']) {
        if (prompts[key].length > 1000 || !prompts[key].every(point)) invalidField(errorId, `${key}は0以上の数値の座標[x,y]を1000件以内で指定してください。`);
    }
    const box = Array.isArray(prompts.obj_bbox) && prompts.obj_bbox.length === 0 ? null : prompts.obj_bbox ?? null;
    if (box !== null && (!Array.isArray(box) || box.length !== 2 || !box.every(point) || box[0][0] >= box[1][0] || box[0][1] >= box[1][1])) {
        invalidField(errorId, 'obj_bboxは左上と右下の座標[[x,y],[x,y]]またはnullを指定してください。');
    }
    return { ...prompts, obj_bbox: box };
}
async function submitModel() {
    const model = editing;
    const updating = Boolean(model?.active_revision);
    const settings = readModelSettings(updating ? model.manifest : null);
    const data = new FormData();
    if (updating) {
        const patch = {};
        for (const [key, value] of Object.entries(settings)) {
            const changed = key === 'polygon' ? Object.keys(value).some((name) => value[name] !== model.manifest.polygon?.[name]) :
                JSON.stringify(value) !== JSON.stringify(model.manifest[key]);
            if (changed) patch[key] = value;
        }
        if (Object.keys(patch).length) data.set('manifest', JSON.stringify(patch));
        if ($('code').files.length && $('code').files[0].name !== 'model.py') {
            invalidField('code-error', 'Pythonコードのファイル名はmodel.pyにしてください。');
        }
    } else {
        data.set('manifest', JSON.stringify({ schema_version: 1, ...settings,
            weights: [...$('weights').files].map((weight) => weight.name) }));
    }
    for (const id of ['code', 'sample']) if ($(id).files.length) data.set(id, $(id).files[0]);
    if (updating) {
        for (const { name, input, error } of replacementWeights) {
            if (!input.files.length) continue;
            if (input.files[0].name !== name) {
                error.textContent = `ONNXファイル名は${name}と一致させてください。`; error.hidden = false;
                throw new Error(error.textContent);
            }
            data.append('weights', input.files[0]);
        }
        if (![...data.keys()].length) throw new Error('変更するファイルまたは設定を指定してください。');
        data.set('expected_revision', model.active_revision);
    } else {
        for (const weight of $('weights').files) data.append('weights', weight);
    }
    await sendPackage(updating ? `/api/models/${model.id}/update` : '/api/upload', data);
}
function renderModels() {
    $('models').replaceChildren(); const query = $('search').value.trim().toLocaleLowerCase(); let count = 0;
    for (const model of models) {
        if (query && !`${model.manifest?.name || ''} ${model.owner}`.toLocaleLowerCase().includes(query)) continue;
        const tr = document.createElement('tr'); const op = model.operations?.[0];
        const pending = op && ['queued', 'validating'].includes(op.status);
        const state = model.deleted ? '削除済み' : model.active_revision ? (pending ? '公開中 / 更新検証中' : '公開中') : ({ queued: '検証待ち', validating: '検証中', failed: '検証失敗' }[op?.status] || '未公開');
        const name = document.createElement('td'); const title = document.createElement('a'); title.href = `#model/${model.id}`; title.className = 'model-title'; title.textContent = model.manifest?.name || model.id; name.append(title);
        if (model.manifest?.description) { const description = document.createElement('span'); description.className = 'model-description'; description.textContent = model.manifest.description; name.append(description); }
        const kind = document.createElement('td'); const badge = document.createElement('span'); badge.className = 'model-type';
        const types = new Set((model.manifest?.labels || []).map((label) => label.type));
        badge.textContent = model.manifest?.kind === 'interactor' ? 'Prompting' : types.size > 1 ? 'Mixed' : types.has('polygon') ? 'Segmentation' : types.has('rectangle') ? 'Detection' : types.has('polyline') ? 'Line detection' : types.has('tag') ? 'Classification' : '未設定'; kind.append(badge);
        const author = document.createElement('td'); const owner = document.createElement('span'); owner.className = 'model-owner'; owner.textContent = model.owner; author.append(owner);
        const contact = document.createElement('span'); contact.className = 'model-contact'; contact.textContent = model.manifest?.author_contact || '連絡先は未設定'; author.append(contact);
        const status = document.createElement('td'); const pill = document.createElement('span'); pill.className = `status ${model.deleted ? '' : pending ? 'pending' : model.active_revision ? 'published' : op?.status === 'failed' ? 'failed' : ''}`; pill.textContent = state; status.append(pill);
        tr.append(name, kind, author, status); $('models').append(tr); count++;
    }
    $('catalog-count').textContent = query ? `${models.length}件中 ${count}件を表示` : `${count}件のモデル`;
    if (!count) { const tr = document.createElement('tr'); const td = document.createElement('td'); td.colSpan = 4; td.className = 'empty-state';
        const title = document.createElement('strong'); title.textContent = query ? '一致するモデルがありません' : '登録されたモデルがありません';
        td.append(title); tr.append(td); $('models').append(tr);
    }
}
async function refresh() {
    if (refreshingEpoch === authEpoch || !user) return;
    const epoch = authEpoch; refreshingEpoch = epoch; $('catalog').setAttribute('aria-busy', 'true');
    try {
        const account = await api('/api/me');
        if (!sameAccount(user, account)) { await signedIn(account); notice('CVATで使用中のアカウントに切り替えました。'); return; }
        models = await api('/api/models'); if (user) renderModels();
    } finally { if (refreshingEpoch === epoch) { refreshingEpoch = null; $('catalog').setAttribute('aria-busy', 'false'); } }
}
async function checkSession() {
    if (checkingSession) return;
    checkingSession = true; $('retry-access').disabled = true;
    if (!user) {
        $('access-title').textContent = 'モデル一覧を読み込んでいます'; $('access-help').textContent = ''; $('access-help').hidden = true;
        $('loading-indicator').hidden = false; $('access-panel').setAttribute('aria-busy', 'true'); $('access-actions').hidden = true;
    }
    try {
        const account = await api('/api/me');
        if (!sameAccount(user, account)) await signedIn(account);
    } catch (error) {
        if (!user && !(error instanceof AccessUnavailable) && !(error instanceof StaleSession)) {
            accessError(); throw new AccessUnavailable();
        }
        throw error;
    } finally { checkingSession = false; $('retry-access').disabled = false; }
}
async function recoverAccount() {
    signedOut();
    try { await signedIn(await api('/api/me')); }
    catch (error) { if (!(error instanceof AccessUnavailable) && !(error instanceof StaleSession)) throw error; }
    notice('CVATのアカウントが変わりました。内容を確認してから再度操作してください。', true);
}
async function route() {
    detailSequence++;
    if (!user) return;
    const hash = location.hash.slice(1) || 'models'; const detailMatch = /^model\/([0-9a-f]{20})$/.exec(hash);
    $('operation').hidden = hash === 'models';
    document.title = `${hash === 'register' ? 'モデル登録' : detailMatch ? 'モデル詳細' : 'モデル一覧'} | CVAT`;
    $('catalog').hidden = hash !== 'models'; $('registration').hidden = hash !== 'register'; $('detail').hidden = true;
    $('models-nav').setAttribute('aria-current', hash === 'models' || detailMatch ? 'page' : 'false'); $('register-nav').setAttribute('aria-current', hash === 'register' ? 'page' : 'false');
    if (detailMatch) await selectModel(detailMatch[1]);
    else if (hash === 'models') { detailSequence++; await refresh(); }
    else if (hash !== 'register') location.hash = 'models';
}
async function selectModel(id) {
    const sequence = ++detailSequence; const model = await api(`/api/models/${id}`); if (sequence !== detailSequence || !user) return;
    selected = model; $('detail').hidden = false; $('detail-title').textContent = model.manifest?.name || model.id;
    $('detail-info').textContent = `作者: ${model.owner} / 公開中の版: ${model.active_revision || 'なし'}${model.deleted ? ' / 削除済み' : ''}`;
    $('detail-contact').textContent = `連絡先: ${model.manifest?.author_contact || '未設定'}`; $('detail-description').textContent = model.manifest?.description || '';
    $('detail-labels').textContent = (model.manifest?.labels || []).map((label, index) => `${index}: ${label.name} (ID=${label.id}, ${label.type})`).join('\n');
    const polygon = model.manifest?.polygon;
    const polygonSummary = polygon && model.manifest?.labels?.some((label) => label.type === 'polygon') ? `ポリゴン変換: 頂点の最小距離 ${polygon.min_distance_px} px、輪郭の近似許容誤差 ${polygon.max_error_px} px、最小面積 ${polygon.min_area_px} px²` : '';
    $('detail-polygon').textContent = polygonSummary;
    const managed = Boolean(model.can_manage); $('owner-detail').hidden = !managed;
    for (const id of ['update-model', 'delete-model']) { $(id).hidden = !managed; $(id).disabled = id === 'update-model' && Boolean(model.deleted || !model.active_revision); }
    $('revision').replaceChildren(); $('logs').textContent = ''; $('test-result').textContent = ''; $('test-summary').textContent = ''; $('test-tags').textContent = ''; $('preview').hidden = true;
    if (managed) {
        for (const rev of model.revisions || []) { const option = document.createElement('option'); option.value = rev.revision; option.textContent = `${rev.revision} ${rev.revision === model.active_revision ? '(公開中)' : '(保持中)'} ${rev.manifest.name}`; $('revision').append(option); }
        $('revision').value = model.active_revision || '';
        testRevisionChanged();
        for (const id of ['rollback', 'test-button']) $(id).disabled = Boolean(model.deleted || !model.active_revision);
        await refreshLogs();
    }
}
function testedManifest() {
    return selected?.revisions?.find((item) => item.revision === $('revision').value)?.manifest || selected?.manifest;
}
function testRevisionChanged() {
    const manifest = testedManifest(); const prompting = manifest?.kind === 'interactor';
    $('test-prompts-field').hidden = !prompting; $('test-prompts').disabled = !prompting;
    $('test-prompts').value = prompting ? JSON.stringify(manifest.interactor.sample, null, 2) : ''; fieldError('test-prompts-error', '');
    $('test-tags').textContent = ''; $('test-summary').textContent = ''; $('test-result').textContent = ''; $('preview').hidden = true;
}
async function refreshLogs() {
    const model = selected; if (!model?.can_manage) return; const logs = await api(`/api/models/${model.id}/logs`); if (selected?.id !== model.id) return;
    $('logs').textContent = logs.map((log) => `${log.time} ${log.level.toUpperCase()} ${log.stage} revision=${log.revision} request_id=${log.request_id}\n${log.message}`).join('\n\n') || 'ログはありません。';
}
async function sendPackage(path, form) {
    const epoch = authEpoch;
    if (activeOperation) throw new Error('現在の登録処理が完了してから操作してください。');
    $('submit-model').disabled = true; $('upload-progress').value = 0; $('upload-progress').hidden = false; $('operation').textContent = ''; $('operation').hidden = false;
    try {
        const operation = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest(); xhr.open('POST', endpoint(path));
            for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);
            xhr.upload.onprogress = (event) => { if (epoch === authEpoch && event.lengthComputable) $('upload-progress').value = event.loaded / event.total * 100; };
            xhr.onerror = () => reject(new Error('アップロードに失敗しました。接続を確認してください。'));
            xhr.onload = () => {
                if (epoch !== authEpoch) { reject(new StaleSession()); return; }
                if (xhr.getResponseHeader('X-Registry-Account-Changed') === '1') { recoverAccount().then(() => reject(new StaleSession()), reject); return; }
                try { const body = JSON.parse(xhr.responseText);
                    if (xhr.status === 401) { accessError(401); reject(new AccessUnavailable()); return; }
                    if (xhr.status >= 400) reject(new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))); else resolve(body);
                } catch { reject(new Error(`HTTP ${xhr.status}`)); }
            }; xhr.send(form);
        });
        if (epoch !== authEpoch) throw new StaleSession();
        activeOperation = operation.id; notice('公開前の検証を開始しました。');
        while (activeOperation && user) {
            if (epoch !== authEpoch) throw new StaleSession();
            const status = await api(`/api/operations/${activeOperation}`);
            const statusName = { queued: '検証待ち', validating: '検証中', succeeded: '公開完了', failed: '検証失敗' }[status.status] || '処理中';
            $('operation').textContent = `${statusName}${status.detail ? `\n${status.detail}` : ''}`;
            if (['succeeded', 'failed'].includes(status.status)) {
                activeOperation = ''; await refresh();
                notice(status.status === 'succeeded' ? '公開しました。' : '検証に失敗しました。処理ログを確認してください。', status.status !== 'succeeded');
                if (status.status === 'succeeded') resetForm();
                location.hash = `model/${status.model_id}`; break;
            }
            await new Promise((resolve) => setTimeout(resolve, 1500));
        }
    } finally { if (epoch === authEpoch) { activeOperation = ''; $('submit-model').disabled = false; $('upload-progress').hidden = true; } }
}
async function signedIn(account) {
    signedOut(); user = account; $('identity').textContent = `${account.name}${account.admin ? ' (管理者)' : ''}`; $('avatar').textContent = account.name.slice(0, 1).toLocaleUpperCase();
    $('access-panel').hidden = true; $('access-panel').setAttribute('aria-busy', 'false'); $('workspace').hidden = false; $('account').hidden = false; notice(''); await route();
}
$('retry-access').addEventListener('click', () => run(checkSession));
$('refresh').addEventListener('click', () => run(refresh)); $('search').addEventListener('input', renderModels);
$('new-model').addEventListener('click', () => { resetForm(); $('operation').textContent = ''; notice(''); location.hash = 'register'; });
$('register-nav').addEventListener('click', () => { if (!activeOperation) { resetForm(); $('operation').textContent = ''; notice(''); } });
$('cancel-update').addEventListener('click', () => resetForm()); $('model-kind').addEventListener('change', kindChanged);
$('disable-neg-points').addEventListener('change', negativePointsChanged);
for (const [id, error] of [['min-distance', 'min-distance-help'], ['max-error', 'max-error-help'], ['min-area', 'min-area-help'], ['min-pos-points', 'min-pos-error'], ['box-mode', 'min-pos-error'], ['sample-prompts', 'sample-prompts-help'], ['test-prompts', 'test-prompts-error']]) {
    $(id).addEventListener('input', () => fieldError(error, ''));
}
$('revision').addEventListener('change', testRevisionChanged);
$('add-label').addEventListener('click', () => { const ids = [...$('labels').querySelectorAll('[data-key=id]')].map((x) => Number(x.value)); labelRow({ id: ids.length ? Math.max(...ids) + 1 : 0, name: '' }); });
for (const id of ['code', 'sample']) $(id).addEventListener('change', updateSummary);
$('register-form').addEventListener('submit', (event) => { event.preventDefault(); run(submitModel); });
$('update-model').addEventListener('click', () => { if (selected?.can_manage && selected.active_revision && !selected.deleted) { resetForm(selected); location.hash = 'register'; } });
$('delete-model').addEventListener('click', () => run(async () => {
    if (!selected?.can_manage || !confirm('このモデルのすべての版とファイルを完全に削除します。元には戻せません。処理中の推論・更新の終了を待つ場合があり、一括推論は後続の画像で失敗します。削除しますか？')) return;
    notice('処理中の推論・更新の終了を待ち、モデルのファイルを削除しています。');
    const id = selected.id; await api(`/api/models/${id}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: selected.active_revision }) });
    selected = null; location.hash = 'models'; await route(); notice('モデルを削除しました。');
}));
$('rollback').addEventListener('click', () => run(async () => {
    if (!selected?.can_manage || !$('revision').value || !confirm('選択した版を試験し、公開中の版を切り替えますか？')) return;
    const id = selected.id; await api(`/api/models/${id}/rollback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: selected.active_revision, revision: $('revision').value }) });
    await refresh(); await selectModel(id); notice('公開する版を切り替えました。');
}));
async function base64File(file) { if (file.size > 32 * 1024 ** 2) throw new Error('画像は32 MiB以下にしてください。'); const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ''; for (let i = 0; i < bytes.length; i += 4096) binary += String.fromCharCode(...bytes.subarray(i, i + 4096)); return btoa(binary); }
async function preview(file, result, modelId, revision) {
    const epoch = authEpoch; const bitmap = await createImageBitmap(file, { imageOrientation: 'none' });
    if (epoch !== authEpoch || selected?.id !== modelId || $('revision').value !== revision) { bitmap.close(); return; }
    const canvas = $('preview'); canvas.width = bitmap.width; canvas.height = bitmap.height;
    const context = canvas.getContext('2d'); context.drawImage(bitmap, 0, 0); bitmap.close(); context.strokeStyle = '#e84b40'; context.lineWidth = Math.max(2, canvas.width / 400);
    if (result.mask) {
        context.fillStyle = 'rgba(232,75,64,.3)';
        for (let y = 0; y < result.mask.length; y++) {
            const row = result.mask[y];
            for (let x = 0; x < row.length;) {
                if (!row[x]) { x++; continue; }
                const start = x; while (x < row.length && row[x]) x++;
                context.fillRect(start, y, x - start, 1);
            }
        }
    }
    // Overlay the same polygon outlines that the Prompting editor will use.
    if (Array.isArray(result.polygons)) {
        context.strokeStyle = '#00b8b8';
        for (const points of result.polygons) {
            context.beginPath(); context.moveTo(points[0], points[1]);
            for (let i = 2; i < points.length; i += 2) context.lineTo(points[i], points[i + 1]);
            context.closePath(); context.stroke();
        }
        context.strokeStyle = '#e84b40';
    }
    for (const obj of Array.isArray(result) ? result : []) {
        if (obj.type === 'rectangle') { const [x1, y1, x2, y2] = obj.points; context.strokeRect(x1, y1, x2 - x1, y2 - y1); }
        else if (obj.type === 'polyline') { context.beginPath(); context.moveTo(obj.points[0], obj.points[1]); for (let i = 2; i < obj.points.length; i += 2) context.lineTo(obj.points[i], obj.points[i + 1]); context.stroke(); }
        else if (obj.type === 'polygon') { context.beginPath(); context.moveTo(obj.points[0], obj.points[1]); for (let i = 2; i < obj.points.length; i += 2) context.lineTo(obj.points[i], obj.points[i + 1]); context.closePath(); context.fillStyle = 'rgba(232,75,64,.3)'; context.fill(); context.stroke(); }
    }
    canvas.hidden = false;
}
$('test-form').addEventListener('submit', (event) => { event.preventDefault(); run(async () => {
    if (!selected?.can_manage) return; const model = selected; const file = $('test-image').files[0]; const revision = $('revision').value; $('test-button').disabled = true;
    $('test-tags').textContent = ''; $('test-summary').textContent = ''; $('test-result').textContent = ''; $('preview').hidden = true;
    const manifest = testedManifest();
    try {
        const prompts = manifest?.kind === 'interactor' ? readPrompts('test-prompts') : {};
        const image = await base64File(file);
        const result = await api(`/api/models/${model.id}/test?revision=${encodeURIComponent(revision)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image, ...prompts }),
        });
        if (selected?.id === model.id && $('revision').value === revision) {
            const objects = Array.isArray(result.results) ? result.results : [];
            const tags = objects.filter((object) => object.type === 'tag');
            $('test-tags').textContent = tags.length ? `分類タグ: ${tags.map((tag) => `${tag.label}（信頼度 ${tag.confidence.toFixed(3)}）`).join('、')}` :
                manifest?.labels?.some((label) => label.type === 'tag') ? '分類タグ: なし' : '';
            $('test-summary').textContent = manifest?.kind === 'interactor' ? `出力マスク: ${result.results.mask ? 1 : 0}件。${Array.isArray(result.results.polygons) ? `変換後のポリゴン: ${result.results.polygons.length}件。${result.results.polygons.length === 0 ? '変換条件を満たす領域がありません。' : ''}` : result.results.mask ? 'ポリゴンは頂点数または件数の上限を超えたため生成していません。CVATでは標準の変換を使います。' : ''}` : `出力件数: ${objects.length}件（図形 ${objects.length - tags.length}件、タグ ${tags.length}件）。`;
            $('test-result').textContent = JSON.stringify({ request_id: result.request_id, revision: result.revision, objects: Array.isArray(result.results) ? result.results.length : result.results.mask ? 1 : 0, results: result.results }, null, 2);
            await preview(file, result.results, model.id, revision);
        }
    } finally { $('test-button').disabled = Boolean(selected?.deleted || !selected?.active_revision); await refreshLogs(); }
}); });
$('refresh-logs').addEventListener('click', () => run(refreshLogs));
$('download-logs').addEventListener('click', () => run(async () => {
    if (!selected?.can_manage) return;
    const epoch = authEpoch; const id = selected.id;
    const response = await fetch(endpoint(`/api/models/${id}/logs.txt`), { headers: authHeaders(), credentials: 'same-origin', cache: 'no-store' });
    if (epoch !== authEpoch) throw new StaleSession();
    if (response.status === 401) { accessError(401); throw new AccessUnavailable(); }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob(); if (epoch !== authEpoch || selected?.id !== id) throw new StaleSession();
    const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `model-${id}-logs.jsonl`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
window.addEventListener('hashchange', () => run(route));
async function resume() {
    if (!user || $('catalog').hidden || refreshingEpoch === authEpoch) await checkSession();
    else await refresh();
}
window.addEventListener('focus', () => run(resume));
document.addEventListener('visibilitychange', () => { if (!document.hidden) run(resume); });
setInterval(() => { if (user && !document.hidden) run(resume); }, 5000);
resetForm();
run(checkSession);
