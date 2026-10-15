// SPDX-License-Identifier: MIT
import { thresholdParameters } from './model-options';

export interface Scope { type: 'task' | 'project'; id: number }
export interface Condition { mode: 'any' | 'all' | 'none' | 'untagged'; labelIds: number[] }
export interface LabelSpec {
    id?: number;
    name: string;
    type: string;
    parent?: number | null;
    color?: string;
    attributes?: { name: string; input_type?: string; values?: string[] }[];
    sublabels?: LabelSpec[];
}
export interface Model { id: string | number; name: string; kind: string; labels: LabelSpec[] }
export interface TaskInfo { id: number; name: string; size: number; dimension: string; project_id: number | null }
export interface Meta {
    size: number;
    deleted_frames?: number[];
    frames?: { name: string }[];
}
export interface Tag { frame: number; label_id: number }
export interface Annotation extends Tag {
    id?: number | null;
    elements?: Annotation[];
    [key: string]: unknown;
}
export interface Collection { tags: Annotation[]; shapes: Annotation[]; tracks: unknown[] }
export interface Frame { key: string; taskId: number; frame: number; name: string }
export interface Plan { scope: Scope; condition: Condition; tasks: TaskInfo[]; frames: Frame[] }
export interface MappingItem { name: string; attributes: Record<string, string>; sublabels?: Mapping }
export interface Mapping { [name: string]: MappingItem }
export interface API {
    get<T>(path: string, params?: Record<string, string | number>): Promise<T>;
    infer(taskId: number, modelId: string | number, args: {
        frame: number; threshold?: number; mapping: Mapping; conv_mask_to_poly: boolean;
    }): Promise<Collection>;
    append(taskId: number, data: Collection): Promise<void>;
}
export interface Outcome { taskId: number; frame: number; status: 'saved' | 'empty' | 'skipped' | 'failed' }
export interface Report { outcomes: Outcome[]; cancelled: boolean; error?: string }

function positiveID(value: number): boolean { return Number.isSafeInteger(value) && value > 0; }

export function validateCondition(condition: Condition): void {
    if (!['any', 'all', 'none', 'untagged'].includes(condition.mode) ||
        !Array.isArray(condition.labelIds) || !condition.labelIds.every(positiveID) ||
        (condition.mode !== 'untagged' && !condition.labelIds.length)) {
        throw new Error('タグ条件を確認してください。タグ条件には1個以上のラベルが必要です。');
    }
}

class CancelledError extends Error {
    constructor() { super('処理を中止しました。'); }
}

async function checkedGet<T>(
    api: API, path: string, params: Record<string, string | number>, cancelled: () => boolean,
): Promise<T> {
    if (cancelled()) throw new CancelledError();
    const result = await api.get<T>(path, params);
    if (cancelled()) throw new CancelledError();
    return result;
}

export async function getAll<T extends { id: number }>(
    api: API, path: string, params: Record<string, string | number>,
    cancelled: () => boolean = () => false,
): Promise<T[]> {
    const result: T[] = [];
    const ids = new Set<number>();
    let expected: number | undefined;
    for (let page = 1; ; page++) {
        const data = await checkedGet<{ count: number; next: string | null; results: T[] }>(
            api, path, { ...params, page_size: 100, page }, cancelled,
        );
        if (!Number.isSafeInteger(data.count) || data.count < 0 || !Array.isArray(data.results) ||
            (expected !== undefined && data.count !== expected)) {
            throw new Error('一覧が取得中に変更されたか、応答が不正です。プレビューをやり直してください。');
        }
        expected = data.count;
        for (const item of data.results) {
            if (!positiveID(item.id) || ids.has(item.id)) throw new Error('一覧に不正または重複したIDがあります。');
            ids.add(item.id);
            result.push(item);
        }
        if (result.length === expected) return result;
        if (!data.results.length || result.length > expected || !data.next) {
            throw new Error('一覧を最後まで取得できませんでした。');
        }
        // Never follow a server-provided URL to a different origin.
    }
}

export async function readLabels(
    api: API, scope: Scope, cancelled: () => boolean = () => false,
): Promise<LabelSpec[]> {
    if (!positiveID(scope.id) || !['task', 'project'].includes(scope.type)) throw new Error('対象IDが不正です。');
    return (await getAll<LabelSpec & { id: number }>(api, '/labels', { [`${scope.type}_id`]: scope.id }, cancelled))
        .filter((label) => !label.parent);
}

function checkLabels(labels: LabelSpec[], condition: Condition): void {
    validateCondition(condition);
    const ids = new Set(labels.map((label) => label.id));
    if (condition.mode !== 'untagged' && condition.labelIds.some((id) => !ids.has(id))) {
        throw new Error('選択したタグラベルが削除または変更されました。プレビューをやり直してください。');
    }
}

export function selectFrames(meta: Meta, tags: Tag[], condition: Condition): number[] {
    validateCondition(condition);
    if (!Number.isSafeInteger(meta.size) || meta.size < 0 || !Array.isArray(tags)) {
        throw new Error('フレーム情報またはタグの応答が不正です。');
    }
    const validFrame = (frame: number): boolean => Number.isSafeInteger(frame) && frame >= 0 && frame < meta.size;
    if (!Array.isArray(meta.deleted_frames ?? []) || !(meta.deleted_frames ?? []).every(validFrame)) {
        throw new Error('削除フレーム情報が不正です。');
    }
    const deleted = new Set(meta.deleted_frames ?? []);
    const byFrame = new Map<number, Set<number>>();
    for (const tag of tags) {
        if (!validFrame(tag.frame) || !positiveID(tag.label_id)) throw new Error('タグのフレーム番号が不正です。');
        const labels = byFrame.get(tag.frame) ?? new Set<number>();
        labels.add(tag.label_id);
        byFrame.set(tag.frame, labels);
    }
    const result: number[] = [];
    // CVAT annotations and lambda calls use task-relative indices, NOT source
    // start_frame + index * frame_step. Deleted frames use these same indices.
    for (let frame = 0; frame < meta.size; frame++) {
        if (deleted.has(frame)) continue;
        const labels = byFrame.get(frame) ?? new Set<number>();
        const matches = condition.mode === 'untagged' ? labels.size === 0 :
            condition.mode === 'all' ? condition.labelIds.every((id) => labels.has(id)) :
                condition.mode === 'none' ? condition.labelIds.every((id) => !labels.has(id)) :
                    condition.labelIds.some((id) => labels.has(id));
        if (matches) result.push(frame);
    }
    return result;
}

function checkTask(task: TaskInfo, scope: Scope): void {
    if (!positiveID(task.id) || task.dimension !== '2d' || !Number.isSafeInteger(task.size) || task.size < 0 ||
        (scope.type === 'task' ? task.id !== scope.id : task.project_id !== scope.id)) {
        throw new Error(`Task #${task.id}: 対象外、未初期化、または2D以外のタスクです。`);
    }
}

export async function buildPlan(
    api: API, scope: Scope, condition: Condition, cancelled: () => boolean = () => false,
): Promise<Plan> {
    checkLabels(await readLabels(api, scope, cancelled), condition);
    const tasks = scope.type === 'task' ? [await checkedGet<TaskInfo>(api, `/tasks/${scope.id}`, {}, cancelled)] :
        await getAll<TaskInfo>(api, '/tasks', { project_id: scope.id, sort: 'id' }, cancelled);
    const frames: Frame[] = [];
    for (const task of tasks) {
        if (cancelled()) throw new CancelledError();
        checkTask(task, scope);
        const meta = await checkedGet<Meta>(api, `/tasks/${task.id}/data/meta`, {}, cancelled);
        if (meta.size !== task.size) throw new Error(`Task #${task.id}: フレーム数が変更されています。`);
        const annotations = await checkedGet<Collection>(api, `/tasks/${task.id}/annotations`, {}, cancelled);
        for (const frame of selectFrames(meta, annotations.tags, condition)) {
            frames.push({
                key: `${task.id}:${frame}`, taskId: task.id, frame,
                name: meta.frames?.[frame]?.name ?? `frame ${frame}`,
            });
        }
        // Do not retain masks/shapes or image pixels in a project-wide plan.
    }
    if (cancelled()) throw new CancelledError();
    return { scope: { ...scope }, condition: { ...condition, labelIds: [...condition.labelIds] }, tasks, frames };
}

export function compatible(model: LabelSpec, target: LabelSpec): boolean {
    return model.type === target.type ||
        (model.type === 'any' && target.type !== 'skeleton') ||
        (target.type === 'any' && model.type !== 'skeleton') ||
        (['mask', 'polygon'].includes(model.type) && ['mask', 'polygon'].includes(target.type));
}

// Validate the existing LabelsMapper's explicit selection against freshly read
// labels. Never silently rebuild it by name: that would lose attribute/point
// selections and could turn an empty mapping into the server's auto-mapping.
export function buildMapping(
    models: LabelSpec[], targets: LabelSpec[], choices: Mapping, allowEmpty = false,
): Mapping {
    const pairs: [string, MappingItem][] = [];
    for (const [modelName, choice] of Object.entries(choices)) {
        const model = models.find((label) => label.name === modelName);
        const target = targets.find((label) => label.name === choice?.name);
        if (!model || !target || !compatible(model, target)) {
            throw new Error(`ラベル対応が不正です: ${modelName}`);
        }
        const attributes = Object.entries(choice.attributes);
        for (const [source, destination] of attributes) {
            if (!model.attributes?.some((attr) => attr.name === source) ||
                !target.attributes?.some((attr) => attr.name === destination)) {
                throw new Error(`属性対応が不正です: ${modelName}.${source}`);
            }
        }
        const item: MappingItem = { name: target.name, attributes: Object.fromEntries(attributes) };
        if (model.type === 'skeleton') {
            if (!choice.sublabels) throw new Error('Skeleton要素の対応が必要です。');
            item.sublabels = buildMapping(model.sublabels ?? [], target.sublabels ?? [], choice.sublabels, true);
        } else if (choice.sublabels !== undefined) {
            throw new Error('Skeleton以外のラベルに要素の対応は指定できません。');
        }
        pairs.push([modelName, item]);
    }
    if (!pairs.length && !allowEmpty) throw new Error('出力ラベルを1個以上対応付けてください。');
    return Object.fromEntries(pairs);
}

export function checkedOutput(data: Collection, frame: number, allowedLabels: Set<number>): Collection {
    if (!data || !Array.isArray(data.tags) || !Array.isArray(data.shapes) ||
        !Array.isArray(data.tracks) || data.tracks.length) {
        throw new Error('単一フレーム推論の応答が不正です。追跡モデルは対象外です。');
    }
    function check(annotation: Annotation, root: boolean): void {
        if (annotation.frame !== frame || (annotation.id !== undefined && annotation.id !== null) ||
            !positiveID(annotation.label_id) || (root && !allowedLabels.has(annotation.label_id))) {
            throw new Error('推論結果に対象外のフレーム、既存ID、または未対応ラベルがあります。保存しません。');
        }
        if (annotation.elements !== undefined) {
            if (!Array.isArray(annotation.elements)) throw new Error('Skeleton要素が不正です。');
            annotation.elements.forEach((element) => check(element, false));
        }
    }
    [...data.tags, ...data.shapes].forEach((annotation) => check(annotation, true));
    return { tags: data.tags, shapes: data.shapes, tracks: [] };
}

export async function executePlan(
    api: API, plan: Plan, selected: Set<string>, model: Model,
    choices: Mapping, threshold: number | null, convMaskToPoly: boolean,
    cancelled: () => boolean, progress: (report: Report) => void,
): Promise<Report> {
    const report: Report = { outcomes: [], cancelled: false };
    let current: Frame | undefined;
    let saving = false;
    const publish = (): void => progress({ ...report, outcomes: [...report.outcomes] });
    try {
        if (model.kind !== 'detector') throw new Error('単一画像用モデルを指定してください。');
        const parameters = thresholdParameters(model, threshold);
        const known = new Set(plan.frames.map((frame) => frame.key));
        if (!selected.size || [...selected].some((key) => !known.has(key))) throw new Error('対象フレームの選択が不正です。');
        if (cancelled()) { report.cancelled = true; return report; }
        const labels = await readLabels(api, plan.scope, cancelled);
        checkLabels(labels, plan.condition);
        const mapping = buildMapping(model.labels, labels, choices);
        const mappedNames = new Set(Object.values(mapping).map((item) => item.name));
        const allowed = new Set(labels.filter((label) => mappedNames.has(label.name)).map((label) => label.id as number));
        for (const task of plan.tasks) {
            const targets = plan.frames.filter((frame) => frame.taskId === task.id && selected.has(frame.key));
            if (!targets.length) continue;
            if (cancelled()) { report.cancelled = true; break; }
            current = undefined;
            const requests = await checkedGet<{ status: string; function: { task: number } }[]>(api, '/lambda/requests', {}, cancelled);
            if (!Array.isArray(requests)) throw new Error('実行中の自動アノテーションを確認できません。');
            if (requests.some((request) => request.function?.task === task.id &&
                ['queued', 'started', 'deferred', 'scheduled'].includes(request.status))) {
                throw new Error(`Task #${task.id}: 通常の自動アノテーションが実行中です。`);
            }
            const latest = await checkedGet<TaskInfo>(api, `/tasks/${task.id}`, {}, cancelled);
            checkTask(latest, plan.scope);
            const meta = await checkedGet<Meta>(api, `/tasks/${task.id}/data/meta`, {}, cancelled);
            if (latest.size !== task.size || meta.size !== task.size) throw new Error(`Task #${task.id}: フレーム数が変更されています。`);
            const annotations = await checkedGet<Collection>(api, `/tasks/${task.id}/annotations`, {}, cancelled);
            const eligible = new Set(selectFrames(meta, annotations.tags, plan.condition));
            for (const target of targets) {
                if (cancelled()) { report.cancelled = true; break; }
                current = target;
                if (!eligible.has(target.frame)) {
                    report.outcomes.push({ taskId: task.id, frame: target.frame, status: 'skipped' });
                    publish();
                    continue;
                }
                const result = await api.infer(task.id, model.id, {
                    frame: target.frame, ...parameters, mapping, conv_mask_to_poly: convMaskToPoly,
                });
                if (cancelled()) { report.cancelled = true; break; }
                const output = checkedOutput(result, target.frame, allowed);
                if (output.tags.length || output.shapes.length) {
                    saving = true;
                    await api.append(task.id, output);
                    saving = false;
                    report.outcomes.push({ taskId: task.id, frame: target.frame, status: 'saved' });
                } else {
                    report.outcomes.push({ taskId: task.id, frame: target.frame, status: 'empty' });
                }
                publish();
            }
            if (report.cancelled) break;
        }
    } catch (error) {
        if (error instanceof CancelledError) {
            report.cancelled = true;
        } else {
            const message = error instanceof Error ? error.message : String(error);
            report.error = saving ? `${message} 保存結果が不明な可能性があります。自動再試行は行いません。対象フレームを確認してください。` : message;
            if (current) report.outcomes.push({ taskId: current.taskId, frame: current.frame, status: 'failed' });
        }
    }
    if (cancelled()) report.cancelled = true;
    publish();
    return report;
}
