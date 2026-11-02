// SPDX-License-Identifier: MIT
import { DICOM_FORMAT, ImportError } from './messages';

export type ImportMode = 'replace' | 'append';
export interface ImportFormat { name: string; enabled: boolean; dimension?: string }
export type HTTPRequest = {
    method: 'GET' | 'POST'; url: string; params: Record<string, string>;
    data?: FormData; signal: AbortSignal; timeout?: number;
    'axios-retry': { retries: number };
};
export type Send = (config: HTTPRequest) => Promise<{ data: unknown }>;

export function selectFormats(formats: ImportFormat[], dimension: string): string[] {
    return formats.filter((format) => format.enabled && typeof format.name === 'string' &&
        !format.name.startsWith('Task Guide ') && format.name !== 'DICOM 1.0' &&
        (!dimension || format.dimension === dimension))
        .map((format) => format.name).sort((a, b) => a.localeCompare(b));
}

export function createAPI(send: Send, base: string, projectID: number, org: string,
    identity: () => string, dimension = ''): {
    formats: () => Promise<string[]>;
    start: (file: File, format: string, mode: ImportMode) => Promise<string>;
    dispose: () => void;
} {
    const controller = new AbortController();
    const owner = identity();
    let submitted = false;
    let available: string[] = [];
    const check = (): void => {
        if (!owner || identity() !== owner || controller.signal.aborted) {
            controller.abort();
            throw new ImportError('アカウントまたは組織が変更されました。画面を開き直してください。');
        }
    };
    const request = async (config: Omit<HTTPRequest, 'params' | 'signal' | 'axios-retry'>,
        params: Record<string, string> = {}): Promise<unknown> => {
        check();
        const response = await send({
            ...config,
            params: { ...params, org },
            signal: controller.signal,
            'axios-retry': { retries: 0 },
        });
        check();
        return response.data;
    };
    return {
        dispose: () => controller.abort(),
        formats: async (): Promise<string[]> => {
            const response = await request({ method: 'GET', url: `${base}/server/annotation/formats` }) as {
                importers?: ImportFormat[];
            };
            const importers = response?.importers;
            if (!Array.isArray(importers) || !importers.some((format) =>
                format.name === DICOM_FORMAT && format.enabled)) {
                throw new ImportError('サーバー側のProject Import annotationsが有効ではありません。');
            }
            available = selectFormats(importers, dimension);
            if (!available.length) throw new ImportError('このProjectで利用できる取り込み形式がありません。');
            return [...available];
        },
        start: async (file, format, mode): Promise<string> => {
            check();
            if (submitted) throw new ImportError('送信済みです。再送する前にRequestsで結果を確認してください。');
            if (!available.includes(format) || (mode !== 'replace' && mode !== 'append')) {
                throw new ImportError('取り込み形式と更新方法を選択してください。');
            }
            if (!file.size || !file.name.toLowerCase().endsWith('.zip')) {
                throw new ImportError('空でないZIPファイルを選択してください。');
            }
            submitted = true;
            const form = new FormData();
            form.append('annotation_file', file);
            try {
                const response = await request({
                    method: 'POST', url: `${base}/projects/${projectID}/annotations`,
                    data: form, timeout: 0,
                }, { format, import_mode: mode, location: 'local' }) as { rq_id?: string };
                if (!response || typeof response.rq_id !== 'string' || !response.rq_id) {
                    throw new ImportError('取り込み受付を確認できませんでした。再送する前にRequestsを確認してください。');
                }
                return response.rq_id;
            } catch {
                // The upload can have reached the server even when the response was lost.
                // Preserve the submission guard and never retry this request automatically.
                throw new ImportError('取り込み受付を確認できませんでした。再送する前にRequestsを確認してください。');
            }
        },
    };
}
export type API = ReturnType<typeof createAPI>;
