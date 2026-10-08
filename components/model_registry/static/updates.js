/* SPDX-License-Identifier: MIT */
/* Classic script loaded after app.js; keeps the existing session/upload safeguards. */
'use strict';
(() => {
    const originalReset = resetForm;
    let replacements = [];
    const panel = document.createElement('section');
    panel.id = 'retained-model-files';
    panel.hidden = true;
    $('weights').parentElement.after(panel);
    const originalWeightsLabel = $('weights').parentElement;
    const summary = document.createElement('p');
    summary.setAttribute('role', 'status'); summary.className = 'help';
    $('submit-model').before(summary);

    resetForm = function resetUpdateForm(model = null) {
        originalReset(model);
        const updating = Boolean(model?.active_revision);
        panel.hidden = !updating;
        originalWeightsLabel.hidden = updating;
        panel.replaceChildren();
        summary.textContent = '';
        replacements = [];
        for (const id of ['code', 'weights', 'sample']) $(id).required = !updating;
        $('submit-model').textContent = updating ? '変更を検証して更新' : '検証して公開';
        if (!updating) return;
        const help = document.createElement('p');
        help.textContent = '未選択のファイルと未変更の設定は公開中の版から引き継ぎます。Pythonコードとサンプル画像も再アップロード不要です。検証に失敗した場合は公開中の版を維持します。';
        panel.append(help);
        for (const name of model.manifest.weights) {
            const label = document.createElement('label');
            label.textContent = `ONNX: ${name}（同じファイル名で置換、未選択なら引き継ぎ）`;
            const input = document.createElement('input');
            input.type = 'file'; input.accept = '.onnx';
            input.setAttribute('aria-label', `${name}を置換するONNXファイル`);
            label.append(input); panel.append(label);
            replacements.push({ name, input });
            input.addEventListener('change', updateSummary);
        }
        updateSummary();
    };
    function updateSummary() {
        if (!editing?.active_revision) { summary.textContent = ''; return; }
        const replaced = replacements.filter(({ input }) => input.files.length).map(({ name }) => name);
        if ($('code').files.length) replaced.push('model.py');
        if ($('sample').files.length) replaced.push('サンプル画像');
        summary.textContent = `基準の版: ${editing.active_revision}。置換するファイル: ${replaced.join('、') || 'なし（設定のみ更新可能）'}。その他のファイルは引き継ぎます。`;
    }
    for (const id of ['code', 'sample']) $(id).addEventListener('change', updateSummary);
    $('register-form').addEventListener('submit', (event) => {
        if (!editing?.active_revision) return;
        event.preventDefault(); event.stopImmediatePropagation();
        run(async () => {
            const model = editing;
            const before = model.manifest;
            const patch = {};
            for (const [key, value] of [
                ['name', $('name').value.trim()], ['description', $('description').value],
                ['author_contact', $('author-contact').value.trim()],
            ]) if (value !== before[key]) patch[key] = value;
            const kind = $('model-kind').value;
            const oldKind = before.labels[0].type;
            const labels = [...$('labels').children].map((row) => {
                const id = Number(row.querySelector('[data-key=id]').value);
                const previous = before.labels.find((label) => label.id === id);
                return { id, name: row.querySelector('[data-key=name]').value.trim(),
                    type: kind === oldKind ? (previous?.type || kind) : kind };
            });
            if (JSON.stringify(labels) !== JSON.stringify(before.labels)) patch.labels = labels;
            if (kind === 'polygon') {
                const polygon = { min_distance_px: Number($('min-distance').value), spacing_percent: Number($('spacing-percent').value), min_area_px: Number($('min-area').value) };
                if (Object.keys(polygon).some((key) => polygon[key] !== before.polygon?.[key])) patch.polygon = polygon;
            }
            const data = new FormData();
            if (Object.keys(patch).length) data.set('manifest', JSON.stringify(patch));
            if ($('code').files.length && $('code').files[0].name !== 'model.py') {
                throw new Error('Pythonコードのファイル名はmodel.pyにしてください。');
            }
            for (const id of ['code', 'sample']) if ($(id).files.length) data.set(id, $(id).files[0]);
            for (const { name, input } of replacements) {
                if (!input.files.length) continue;
                if (input.files[0].name !== name) throw new Error(`ONNXファイル名は${name}と一致させてください。`);
                data.append('weights', input.files[0]);
            }
            if (![...data.keys()].length) throw new Error('変更するファイルまたは設定を指定してください。');
            await sendPackage(`/api/models/${model.id}/update`, data);
        });
    }, true);
})();
