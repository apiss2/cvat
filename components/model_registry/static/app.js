/* SPDX-License-Identifier: MIT */
'use strict';
const $ = (id) => document.getElementById(id);
const base = new URL('./', location.href);
let user = null, authEpoch = 0, checkingSession = false;
class StaleSession extends Error {}
class AccessUnavailable extends Error {}
let selected = null, editing = null, models = [], activeOperation = '', refreshingEpoch = null, detailSequence = 0;
const endpoint = (path) => new URL(path.replace(/^\//, ''), base).href;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function authHeaders() { return { 'X-Registry-Request': '1', ...(user ? { 'X-Registry-User-ID': String(user.id) } : {}) }; }
function sameAccount(left, right) { return Boolean(left && right && left.id === right.id && left.name === right.name && left.admin === right.admin); }
function signedOut() {
    authEpoch++; user = null; selected = null; editing = null; models = []; activeOperation = ''; detailSequence++;
    $('workspace').hidden = true; $('operation').hidden = true; $('access-panel').hidden = false; $('account').hidden = true; $('identity').textContent = ''; $('avatar').textContent = '';
    $('models').replaceChildren();
    for (const id of ['logs', 'test-result', 'test-tags', 'operation', 'detail-title', 'detail-info', 'detail-contact', 'detail-description', 'detail-labels', 'detail-polygon', 'catalog-count']) $(id).textContent = '';
    for (const id of ['model-count', 'published-count', 'my-model-count']) $(id).textContent = '0';
    $('search').value = ''; $('revision').replaceChildren(); $('preview').hidden = true; $('preview').width = 0; $('preview').height = 0; $('test-form').reset();
    $('submit-model').disabled = false; $('upload-progress').hidden = true; resetForm();
}
function accessError(status) {
    signedOut(); notice('');
    const messages = {
        401: ['CVATのセッションを確認できません', 'CVATへ戻ってアカウントの状態を確認し、モデル管理を開き直してください。'],
        403: ['CVATのアカウントを利用できません', '管理者にアカウントと権限の確認を依頼してください。'],
        429: ['CVATへのアクセスが一時的に制限されています', 'しばらく待ってから再試行してください。'],
        502: ['CVATとの連携に失敗しました', 'CVATがモデル管理サーバーからの接続を受け付けませんでした。管理者に接続設定の確認を依頼してください。'],
    };
    const [title, help] = messages[status] || ['CVATとの接続を確認できません', '時間をおいて再試行してください。改善しない場合は管理者に接続設定の確認を依頼してください。'];
    $('access-title').textContent = title; $('access-help').textContent = help;
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
function kindChanged() {
    const segmentation = $('model-kind').value === 'polygon'; $('polygon-options').hidden = !segmentation;
    for (const id of ['min-distance', 'spacing-percent', 'min-area']) $(id).disabled = !segmentation;
    $('label-help').textContent = $('model-kind').value === 'tag' ? 'Classificationの返却値はTagの配列です。Tagのclass_idを登録するクラスIDに対応付け、画像または動画の各フレームにタグを付けます。単一分類は1個、複数分類は複数個、該当なしは空配列を返してください。分類の選択としきい値はmodel.pyに記述します。画像全体を分類する場合はCVATで範囲を切り出さずに実行してください。' : segmentation ? 'Segmentationの返却値は二値配列 (N,H,W) です。Nの各面を、下のラベルの上からの順番に対応付けます。判定に使うしきい値はmodel.pyに記述します。' : 'Detectionの返却値はBoxの配列です。Boxのclass_idを、登録するクラスIDに対応付けます。信頼度の判定と重複した矩形の除去はmodel.pyに記述します。';
}
function labelRow(value = { id: 0, name: '' }) {
    const row = document.createElement('div'); row.className = 'label-row';
    for (const [key, title, type] of [['id', 'クラスID', 'number'], ['name', 'ラベル名', 'text']]) {
        const label = document.createElement('label'); label.textContent = title; const input = document.createElement('input');
        input.dataset.key = key; input.type = type; input.required = true;
        if (type === 'number') { input.min = '0'; input.max = String(2 ** 31 - 1); input.step = '1'; } else input.maxLength = 128;
        input.value = value[key]; label.append(input); row.append(label);
    }
    row.append(button('削除', () => { if ($('labels').children.length <= 1) throw new Error('ラベルは1件以上必要です。'); row.remove(); })); $('labels').append(row);
}
function resetForm(model = null) {
    editing = model; $('register-form').reset(); $('labels').replaceChildren();
    $('form-title').textContent = model ? `モデルの更新: ${model.manifest?.name || model.id}` : 'モデルの新規登録';
    $('name').value = model?.manifest?.name || ''; $('description').value = model?.manifest?.description || ''; $('author-contact').value = model?.manifest?.author_contact || '';
    const labelType = model?.manifest?.labels?.[0]?.type;
    $('model-kind').value = ['rectangle', 'tag'].includes(labelType) ? labelType : 'polygon';
    const polygon = model?.manifest?.polygon || {}; $('min-distance').value = polygon.min_distance_px ?? 2; $('spacing-percent').value = polygon.spacing_percent ?? 1; $('min-area').value = polygon.min_area_px ?? 10;
    (model?.manifest?.labels || [{ id: 0, name: 'object' }]).forEach(labelRow); $('cancel-update').hidden = !model; kindChanged();
}
function renderModels() {
    $('models').replaceChildren(); const query = $('search').value.trim().toLocaleLowerCase(); let count = 0;
    $('model-count').textContent = models.length;
    $('published-count').textContent = models.filter((model) => model.active_revision && !model.deleted).length;
    $('my-model-count').textContent = models.filter((model) => model.owner === user?.name).length;
    for (const model of models) {
        if (query && !`${model.manifest?.name || ''} ${model.owner}`.toLocaleLowerCase().includes(query)) continue;
        const tr = document.createElement('tr'); const op = model.operations?.[0];
        const pending = op && ['queued', 'validating'].includes(op.status);
        const state = model.deleted ? '削除済み' : model.active_revision ? (pending ? '公開中 / 更新検証中' : '公開中') : ({ queued: '検証待ち', validating: '検証中', failed: '検証失敗' }[op?.status] || '未公開');
        const name = document.createElement('td'); const title = document.createElement('span'); title.className = 'model-title'; title.textContent = model.manifest?.name || model.id; name.append(title);
        if (model.manifest?.description) { const description = document.createElement('span'); description.className = 'model-description'; description.textContent = model.manifest.description; name.append(description); }
        const kind = document.createElement('td'); const badge = document.createElement('span'); badge.className = 'model-type';
        const types = new Set((model.manifest?.labels || []).map((label) => label.type));
        badge.textContent = types.size > 1 ? 'Mixed' : types.has('polygon') ? 'Segmentation' : types.has('rectangle') ? 'Detection' : types.has('tag') ? 'Classification' : '未設定'; kind.append(badge);
        const author = document.createElement('td'); const owner = document.createElement('span'); owner.className = 'model-owner'; owner.textContent = model.owner; author.append(owner);
        const contact = document.createElement('span'); contact.className = 'model-contact'; contact.textContent = model.manifest?.author_contact || '連絡先は未設定'; author.append(contact);
        const status = document.createElement('td'); const pill = document.createElement('span'); pill.className = `status ${model.deleted ? '' : pending ? 'pending' : model.active_revision ? 'published' : op?.status === 'failed' ? 'failed' : ''}`; pill.textContent = state; status.append(pill);
        const action = document.createElement('td'); const detail = button('詳細を見る →', () => { location.hash = `model/${model.id}`; }); detail.className = 'row-action'; detail.setAttribute('aria-label', `${model.manifest?.name || model.id}の詳細を見る`); action.append(detail);
        tr.append(name, kind, author, status, action); $('models').append(tr); count++;
    }
    $('catalog-count').textContent = query ? `${models.length}件中 ${count}件を表示` : `${count}件のモデル`;
    if (!count) { const tr = document.createElement('tr'); const td = document.createElement('td'); td.colSpan = 5; td.className = 'empty-state';
        const title = document.createElement('strong'); title.textContent = query ? '一致するモデルがありません' : '最初のモデルを登録しましょう';
        const help = document.createElement('span'); help.textContent = query ? '別のモデル名や作者で検索してください。' : '登録して公開したモデルは、この一覧からチームに共有されます。';
        td.append(title, help); tr.append(td); $('models').append(tr);
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
        $('access-title').textContent = 'モデル一覧を読み込んでいます'; $('access-help').textContent = '少々お待ちください。';
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
    $('detail-polygon').textContent = polygon && model.manifest?.labels?.[0]?.type === 'polygon' ? `Polygon変換: 最小距離 ${polygon.min_distance_px} px、周長比 ${polygon.spacing_percent}%、最小面積 ${polygon.min_area_px} px²` : '';
    const managed = Boolean(model.can_manage); $('owner-detail').hidden = !managed;
    for (const id of ['update-model', 'delete-model']) { $(id).hidden = !managed; $(id).disabled = Boolean(model.deleted || (id === 'update-model' && !model.active_revision)); }
    $('revision').replaceChildren(); $('logs').textContent = ''; $('test-result').textContent = ''; $('test-tags').textContent = ''; $('preview').hidden = true;
    if (managed) {
        for (const rev of model.revisions || []) { const option = document.createElement('option'); option.value = rev.revision; option.textContent = `${rev.revision} ${rev.revision === model.active_revision ? '(公開中)' : '(保持中)'} ${rev.manifest.name}`; $('revision').append(option); }
        $('revision').value = model.active_revision || '';
        for (const id of ['rollback', 'test-button']) $(id).disabled = Boolean(model.deleted || !model.active_revision);
        await refreshLogs();
    }
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
            const status = await api(`/api/operations/${activeOperation}`); $('operation').textContent = `${status.status}\n${status.detail}\nモデルID=${status.model_id}\n版=${status.revision}\n処理ID=${status.id}`;
            if (['succeeded', 'failed'].includes(status.status)) {
                activeOperation = ''; await refresh();
                notice(status.status === 'succeeded' ? '公開しました。CVATのモデル一覧は通常5秒以内に更新されます。' : '検証に失敗しました。処理ログを確認してください。', status.status !== 'succeeded');
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
$('add-label').addEventListener('click', () => { const ids = [...$('labels').querySelectorAll('[data-key=id]')].map((x) => Number(x.value)); labelRow({ id: ids.length ? Math.max(...ids) + 1 : 0, name: '' }); });
$('register-form').addEventListener('submit', (event) => { event.preventDefault(); run(async () => {
    const weights = [...$('weights').files]; const labels = [...$('labels').children].map((row) => ({ id: Number(row.querySelector('[data-key=id]').value), name: row.querySelector('[data-key=name]').value.trim(), type: $('model-kind').value }));
    const manifest = { schema_version: 1, name: $('name').value.trim(), description: $('description').value, author_contact: $('author-contact').value.trim(), weights: weights.map((w) => w.name), labels };
    if ($('model-kind').value === 'polygon') manifest.polygon = { min_distance_px: Number($('min-distance').value), spacing_percent: Number($('spacing-percent').value), min_area_px: Number($('min-area').value) };
    const data = new FormData(); data.set('manifest', JSON.stringify(manifest)); data.set('code', $('code').files[0]); data.set('sample', $('sample').files[0]); weights.forEach((weight) => data.append('weights', weight)); await sendPackage('/api/upload', data);
}); });
$('update-model').addEventListener('click', () => { if (selected?.can_manage && selected.active_revision && !selected.deleted) { resetForm(selected); location.hash = 'register'; } });
$('delete-model').addEventListener('click', () => run(async () => {
    if (!selected?.can_manage || !confirm('このモデルの全ての版を無効にします。実行中の一括推論は次の画像から失敗する場合があります。削除しますか？')) return;
    const id = selected.id; await api(`/api/models/${id}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: selected.active_revision }) });
    await refresh(); await selectModel(id); notice('モデルを無効にしました。');
}));
$('rollback').addEventListener('click', () => run(async () => {
    if (!selected?.can_manage || !$('revision').value || !confirm('選択した版を試験し、公開中の版を切り替えますか？')) return;
    const id = selected.id; await api(`/api/models/${id}/rollback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: selected.active_revision, revision: $('revision').value }) });
    await refresh(); await selectModel(id); notice('公開する版を切り替えました。');
}));
async function base64File(file) { if (file.size > 32 * 1024 ** 2) throw new Error('画像は32 MiB以下にしてください。'); const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ''; for (let i = 0; i < bytes.length; i += 4096) binary += String.fromCharCode(...bytes.subarray(i, i + 4096)); return btoa(binary); }
async function preview(file, result, modelId) {
    const epoch = authEpoch; const bitmap = await createImageBitmap(file, { imageOrientation: 'none' });
    if (epoch !== authEpoch || selected?.id !== modelId) { bitmap.close(); return; }
    const canvas = $('preview'); canvas.width = bitmap.width; canvas.height = bitmap.height;
    const context = canvas.getContext('2d'); context.drawImage(bitmap, 0, 0); bitmap.close(); context.strokeStyle = '#e84b40'; context.lineWidth = Math.max(2, canvas.width / 400);
    for (const obj of result) {
        if (obj.type === 'rectangle') { const [x1, y1, x2, y2] = obj.points; context.strokeRect(x1, y1, x2 - x1, y2 - y1); }
        else if (obj.type === 'polygon') { context.beginPath(); context.moveTo(obj.points[0], obj.points[1]); for (let i = 2; i < obj.points.length; i += 2) context.lineTo(obj.points[i], obj.points[i + 1]); context.closePath(); context.fillStyle = 'rgba(232,75,64,.3)'; context.fill(); context.stroke(); }
    }
    canvas.hidden = false;
}
$('test-form').addEventListener('submit', (event) => { event.preventDefault(); run(async () => {
    if (!selected?.can_manage) return; const model = selected; const file = $('test-image').files[0]; const revision = $('revision').value; $('test-button').disabled = true;
    $('test-tags').textContent = ''; $('test-result').textContent = ''; $('preview').hidden = true;
    try { const result = await api(`/api/models/${model.id}/test?revision=${encodeURIComponent(revision)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image: await base64File(file) }) });
        if (selected?.id === model.id) {
            const tags = result.results.filter((object) => object.type === 'tag');
            const testedManifest = model.revisions?.find((item) => item.revision === revision)?.manifest || model.manifest;
            $('test-tags').textContent = tags.length ? `分類タグ: ${tags.map((tag) => `${tag.label}（信頼度 ${tag.confidence.toFixed(3)}）`).join('、')}` :
                testedManifest?.labels?.some((label) => label.type === 'tag') ? '分類タグ: なし' : '';
            $('test-result').textContent = JSON.stringify({ request_id: result.request_id, revision: result.revision, objects: result.results.length, results: result.results }, null, 2);
            await preview(file, result.results, model.id);
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
