// SPDX-License-Identifier: MIT
import type { API, Collection } from './engine';

export interface Request {
    method: 'GET' | 'POST' | 'PATCH';
    url: string;
    params: Record<string, string | number>;
    data?: unknown;
    timeout: number;
}
export type Transport = (request: Request) => Promise<{ data: unknown }>;

// Pass CVAT's shared, already configured Axios instance. Do not store tokens or
// create another client that omits CVAT's session, CSRF or organization handling.
export function createAPI(send: Transport, base: string, org: string, identity: () => string): API {
    const owner = identity();
    let invalidated = false;
    const guard = (): void => {
        invalidated = invalidated || !owner || identity() !== owner;
        if (invalidated) throw new Error('利用者または組織が変更されました。画面を開き直してください。');
    };
    const request = async <T>(
        method: Request['method'], path: string, data?: unknown, params: Record<string, string | number> = {},
    ): Promise<T> => {
        guard();
        if (!path.startsWith('/') || path.startsWith('//') || path.includes('..')) throw new Error('APIパスが不正です。');
        const response = await send({
            method, url: `${base.replace(/\/$/, '')}${path}`, data,
            params: { ...params, org }, timeout: method === 'POST' ? 300000 : 60000,
        });
        guard();
        return response.data as T;
    };
    const taskPath = (id: number): string => {
        if (!Number.isSafeInteger(id) || id <= 0) throw new Error('タスクIDが不正です。');
        return `/tasks/${id}`;
    };
    return {
        get: <T>(path: string, params?: Record<string, string | number>) => request<T>('GET', path, undefined, params),
        infer: (taskId, modelId, args) => {
            taskPath(taskId);
            if (!String(modelId) || String(modelId).includes('/') || String(modelId).includes('..')) {
                throw new Error('モデルIDが不正です。');
            }
            return request<Collection>('POST', `/lambda/functions/${encodeURIComponent(String(modelId))}`, {
                ...args, task: taskId,
            });
        },
        append: async (taskId, data) => {
            // Never replace annotations or retry this mutation automatically.
            await request('PATCH', `${taskPath(taskId)}/annotations`, data, { action: 'create' });
        },
    };
}
