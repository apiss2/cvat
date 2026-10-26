// SPDX-License-Identifier: MIT

export interface TaskGuide {
    id: number;
    task_id: number;
    project_id: null;
    markdown: string;
}

export interface Target {
    type: 'tasks' | 'jobs';
    id: number;
    taskId: number;
}

export interface Request {
    method: 'GET';
    url: string;
    params?: { org: string };
    headers: { Accept: string };
    'axios-retry': { retries: number };
    timeout: number;
    signal: AbortSignal;
}

export type Transport = (request: Request) => Promise<{ data: unknown }>;

export interface API {
    read(): Promise<TaskGuide | null>;
    dispose(): void;
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
    const request = async (path: string, params?: Request['params']): Promise<unknown> => {
        guard();
        let data: unknown;
        try {
            ({ data } = await send({
                method: 'GET',
                url: `${base.replace(/\/$/, '')}/${path}`,
                ...(params ? { params } : {}),
                headers: { Accept: 'application/vnd.cvat+json' },
                'axios-retry': { retries: 0 },
                timeout: 30000,
                signal: controller.signal,
            }));
        } catch (reason) {
            guard();
            throw reason;
        }
        guard();
        return data;
    };
    return {
        read: async (): Promise<TaskGuide | null> => {
            const resource = await request(`${target.type}/${target.id}`, { org }) as {
                id?: unknown; task_id?: unknown; guide_id?: unknown; task_guide_id?: unknown;
            } | null;
            if (!resource || resource.id !== target.id ||
                (target.type === 'jobs' && resource.task_id !== target.taskId)) {
                throw new Error('タスク情報の応答を確認できませんでした。');
            }
            // A job's guide_id may refer to its project. Never use it for task information.
            const guideId = target.type === 'tasks' ? resource.guide_id : resource.task_guide_id;
            if (guideId === null) return null;
            if (!Number.isSafeInteger(guideId) || (guideId as number) <= 0) {
                throw new Error('タスク情報の応答を確認できませんでした。');
            }
            // Standard guide retrieval does not accept organization selection parameters.
            const guide = await request(`guides/${guideId}`) as TaskGuide | null;
            if (!guide || guide.id !== guideId || guide.task_id !== target.taskId ||
                guide.project_id !== null || typeof guide.markdown !== 'string') {
                throw new Error('タスク情報の応答を確認できませんでした。');
            }
            return guide;
        },
        dispose: () => { invalidated = true; controller.abort(); },
    };
}

export function errorMessage(reason: unknown): string {
    const response = (reason as { response?: { status?: number; data?: { detail?: unknown } } } | null)?.response;
    if (response?.status === 403) return 'この情報を閲覧する権限がありません。';
    if (response?.status === 404) return 'タスク情報が見つかりません。';
    if (typeof response?.data?.detail === 'string') return response.data.detail;
    return reason instanceof Error ? reason.message : 'タスク情報を取得できませんでした。';
}
