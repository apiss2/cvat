// SPDX-License-Identifier: MIT
export type Mode = 'any' | 'all' | 'none' | 'untagged' | 'all_frames';
export interface FrameFilter { mode: Mode; tag_ids: number[]; start: number; stop: number | null; step: number }
export interface Options { labels: { id: number; name: string }[]; formats: string[] }
export interface Preview {
    count: number;
    revision: string;
    tasks: { id: number; name: string; count: number; total: number }[];
}
export interface Selection {
    filter: FrameFilter;
    format: string;
    saveImages: boolean;
    omitTags: boolean;
}
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
    preview(selection: Selection): Promise<Preview>;
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
    const request = async <T>(path: string, selection?: Selection, revision?: string): Promise<T> => {
        guard();
        const params: Request['params'] = { org };
        if (selection) {
            Object.assign(params, {
                format: selection.format, save_images: selection.saveImages,
                location: 'local', use_default_location: false,
            });
        }
        const body = selection ? {
            filter: selection.filter, omit_tags: selection.omitTags,
            ...(revision ? { expected_revision: revision } : {}),
        } : undefined;
        let data: unknown;
        try {
            ({ data } = await send({
                method: selection ? 'POST' : 'GET',
                url: `${base.replace(/\/$/, '')}/filtered-export/${type}s/${id}/${path}`,
                params, data: body, timeout: 60000, signal: controller.signal,
            }));
        } catch (reason) {
            guard();
            // A duplicate request can refer to an already running export. Never
            // retry a POST automatically, including after a timeout.
            const response = (reason as { response?: { status?: number; data?: { rq_id?: unknown } } } | null)?.response;
            if (path === 'export' && response?.status === 409 && typeof response.data?.rq_id === 'string') {
                data = response.data;
            } else {
                throw reason;
            }
        }
        guard();
        return data as T;
    };
    return {
        options: () => request<Options>('options'),
        preview: (selection) => request<Preview>('preview', selection),
        export: (selection, revision) => request<{ rq_id: string }>('export', selection, revision),
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
