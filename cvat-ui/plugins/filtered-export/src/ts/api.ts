// SPDX-License-Identifier: MIT
export interface AttributeSpec { id: number; name: string; input_type: string; default_value: string; values: string[] }
export interface LabelSpec { id: number; name: string; type: string; attributes: AttributeSpec[]; sublabels: LabelSpec[] }
export interface Options { labels: LabelSpec[]; formats: string[] }
export interface Preview {
    count: number;
    revision: string;
    tasks: { id: number; name: string; count: number; total: number }[];
}
export interface Selection { filters: object[]; format: string; saveImages: boolean }
export interface Request {
    method: 'GET' | 'POST';
    url: string;
    params: Record<string, string | number | boolean>;
    data?: unknown;
    timeout: number;
    signal: AbortSignal;
}
export type Transport = (request: Request) => Promise<{ data: unknown }>;
export interface API {
    options(): Promise<Options>;
    preview(filters: object[]): Promise<Preview>;
    export(selection: Selection, revision: string): Promise<{ rq_id: string }>;
    dispose(): void;
}

export function createAPI(send: Transport, base: string, type: 'task' | 'project', id: number,
    org: string, identity: () => string): API {
    if (!Number.isSafeInteger(id) || id <= 0 || !['task', 'project'].includes(type)) {
        throw new Error('出力対象のIDが不正です。');
    }
    const owner = identity();
    const controller = new AbortController();
    let invalidated = false;
    const guard = (): void => {
        invalidated = invalidated || !owner || identity() !== owner || controller.signal.aborted;
        if (invalidated) throw new Error('利用者または組織が変更されました。画面を開き直してください。');
    };
    const request = async <T>(path: string, body?: object, params: Request['params'] = {}): Promise<T> => {
        guard();
        let data: unknown;
        try {
            ({ data } = await send({
                method: body ? 'POST' : 'GET',
                url: `${base.replace(/\/$/, '')}/filtered-export/${type}s/${id}/${path}`,
                params: { ...params, org }, data: body, timeout: 60000, signal: controller.signal,
            }));
        } catch (reason) {
            guard();
            const response = (reason as { response?: { status?: number; data?: { rq_id?: unknown } } } | null)?.response;
            if (path === 'export' && response?.status === 409 && typeof response.data?.rq_id === 'string') {
                data = response.data;
            } else {
                throw reason; // Never automatically retry a POST, even after a timeout.
            }
        }
        guard();
        return data as T;
    };
    return {
        options: () => request<Options>('options'),
        preview: (filters) => request<Preview>('preview', { filters }, { location: 'local' }),
        export: (selection, revision) => request<{ rq_id: string }>('export', {
            filters: selection.filters, expected_revision: revision,
        }, { format: selection.format, save_images: selection.saveImages, location: 'local' }),
        dispose: () => { invalidated = true; controller.abort(); },
    };
}

export function errorMessage(reason: unknown): string {
    const data = (reason as { response?: { data?: unknown } } | null)?.response?.data;
    if (typeof data === 'string') return data;
    if (Array.isArray(data)) return data.join('\n');
    if (data && typeof data === 'object') return JSON.stringify(data);
    return reason instanceof Error ? reason.message : String(reason);
}
