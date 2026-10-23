// SPDX-License-Identifier: MIT

export interface ContextField {
    name: string;
    value: string;
}

export interface ContextDraft {
    fields: ContextField[];
    notes: string;
}

export interface TaskContext extends ContextDraft {
    task_id: number;
    version: number;
    can_edit: boolean;
}

export interface Target {
    type: 'tasks' | 'jobs';
    id: number;
    taskId: number;
}

export interface Request {
    method: 'GET' | 'PUT';
    url: string;
    params: { org: string };
    headers: { Accept: string };
    'axios-retry': { retries: number };
    data?: ContextDraft & { version: number };
    timeout: number;
    signal: AbortSignal;
}

export type Transport = (request: Request) => Promise<{ data: unknown }>;

export interface API {
    read(): Promise<TaskContext>;
    save(draft: ContextDraft, version: number): Promise<TaskContext>;
    dispose(): void;
}

function parseContext(data: unknown, taskId: number): TaskContext {
    const value = data as TaskContext | null;
    if (!value || value.task_id !== taskId || !Number.isSafeInteger(value.version) || value.version < 0 ||
        typeof value.can_edit !== 'boolean' || typeof value.notes !== 'string' ||
        !Array.isArray(value.fields) || value.fields.some((field) => (
        !field || typeof field.name !== 'string' || typeof field.value !== 'string'
    ))) {
        throw new Error('タスク情報の応答を確認できませんでした。');
    }
    return value;
}

export function createAPI(
    send: Transport,
    base: string,
    target: Target,
    org: string,
    identity: () => string,
): API {
    if (!['tasks', 'jobs'].includes(target.type) ||
        !Number.isSafeInteger(target.id) || target.id <= 0 ||
        !Number.isSafeInteger(target.taskId) || target.taskId <= 0 ||
        (target.type === 'tasks' && target.id !== target.taskId)) {
        throw new Error('タスク情報の対象が不正です。');
    }
    const controller = new AbortController();
    const owner = identity();
    let invalidated = false;
    const guard = (): void => {
        invalidated = invalidated || !owner || identity() !== owner || controller.signal.aborted;
        if (invalidated) throw new Error('画面を開き直してください。');
    };
    const request = async (draft?: ContextDraft, version?: number): Promise<TaskContext> => {
        guard();
        const writing = draft !== undefined;
        const type = writing ? 'tasks' : target.type;
        const id = writing ? target.taskId : target.id;
        let data: unknown;
        try {
            ({ data } = await send({
                method: writing ? 'PUT' : 'GET',
                url: `${base.replace(/\/$/, '')}/task-context/${type}/${id}/`,
                params: { org },
                headers: { Accept: 'application/vnd.cvat+json' },
                'axios-retry': { retries: 0 },
                data: writing ? { fields: draft.fields, notes: draft.notes, version } : undefined,
                timeout: 30000,
                signal: controller.signal,
            }));
        } catch (reason) {
            guard();
            throw reason;
        }
        guard();
        return parseContext(data, target.taskId);
    };
    return {
        read: () => request(),
        save: (draft, version) => {
            if (!Number.isSafeInteger(version) || version < 0) {
                return Promise.reject(new Error('タスク情報を再読み込みしてください。'));
            }
            return request(draft, version);
        },
        dispose: () => { invalidated = true; controller.abort(); },
    };
}

export function responseStatus(reason: unknown): number | undefined {
    return (reason as { response?: { status?: number } } | null)?.response?.status;
}

export function errorMessage(reason: unknown): string {
    const status = responseStatus(reason);
    if (status === 403) return 'この情報を操作する権限がありません。';
    if (status === 404) return 'タスク情報が見つかりません。';
    const data = (reason as { response?: { data?: { detail?: unknown } } } | null)?.response?.data;
    if (typeof data?.detail === 'string') return data.detail;
    return reason instanceof Error ? reason.message : 'タスク情報を取得または保存できませんでした。';
}

export function validateDraft(draft: ContextDraft): string {
    if (draft.fields.length > 64) return '項目は64件まで登録できます。';
    const names = new Set<string>();
    for (let index = 0; index < draft.fields.length; index++) {
        const { name, value } = draft.fields[index];
        if (!name.trim()) return `${index + 1}行目の項目名を入力してください。`;
        if (name.trim().length > 128) return '項目名は128文字以内で入力してください。';
        if (names.has(name.trim())) return '同じ項目名が複数あります。';
        if (value.length > 4096) return '値は4096文字以内で入力してください。';
        names.add(name.trim());
    }
    if (draft.notes.length > 16384) return 'メモは16384文字以内で入力してください。';
    return '';
}
