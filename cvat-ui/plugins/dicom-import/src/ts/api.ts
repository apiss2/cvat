// SPDX-License-Identifier: MIT
export type HTTPRequest = {
    method: 'GET' | 'POST'; url: string; params: Record<string, string>;
    data?: FormData; signal: AbortSignal; timeout?: number;
    'axios-retry': { retries: number };
};
export type Send = (config: HTTPRequest) => Promise<{ data: unknown }>;
export function createAPI(send: Send, base: string, projectID: number, org: string,
    identity: () => string): { available: () => Promise<void>; start: (file: File) => Promise<string>;
        dispose: () => void; signal: AbortSignal } {
    const controller = new AbortController();
    const initial = identity();
    let submitted = false;
    const check = (): void => {
        if (!initial || identity() !== initial || controller.signal.aborted) {
            controller.abort();
            throw new Error('アカウントまたは組織が変更されました。');
        }
    };
    const request = async (config: Omit<HTTPRequest, 'params' | 'signal' | 'axios-retry'>,
        params: Record<string, string> = {}): Promise<unknown> => {
        check();
        const response = await send({ ...config, params: { ...params, org }, signal: controller.signal,
            'axios-retry': { retries: 0 } });
        check();
        return response.data;
    };
    const available = async (): Promise<void> => {
        const data = await request({ method: 'GET', url: `${base}/server/annotation/formats` }) as {
            importers?: { name: string; enabled: boolean }[];
        };
        if (!data?.importers?.some((format) => format.name === 'DICOM 1.0' && format.enabled)) {
            throw new Error('サーバー側のDICOM importが有効ではありません。');
        }
    };
    return {
        signal: controller.signal, available, dispose: () => controller.abort(),
        start: async (file): Promise<string> => {
            check();
            if (submitted) throw new Error('送信済みです。Requestsで結果を確認してください。');
            submitted = true;
            const form = new FormData();
            form.append('dataset_file', file);
            try {
                const data = await request({ method: 'POST', url: `${base}/projects/${projectID}/dataset`,
                    data: form, timeout: 0 }, { format: 'DICOM 1.0', location: 'local' }) as { rq_id?: string };
                if (!data || typeof data.rq_id !== 'string') throw new Error('Invalid request response');
                return data.rq_id;
            } catch (error) {
                if (controller.signal.aborted) throw error;
                // POST may have reached the server. Never retry automatically or unlock this form.
                throw new Error('登録受付を確認できませんでした。再送する前にRequestsを確認してください。');
            }
        },
    };
}
export type API = ReturnType<typeof createAPI>;
