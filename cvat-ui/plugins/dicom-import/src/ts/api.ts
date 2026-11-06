// SPDX-License-Identifier: MIT
import { safePath } from './selection';

export type HTTPRequest = {
    method: 'GET' | 'POST'; url: string; params: Record<string, string>;
    data?: Blob | Record<string, unknown>; headers?: Record<string, string>;
    signal: AbortSignal; timeout?: number;
    onUploadProgress?: (event: { loaded: number }) => void;
    'axios-retry': { retries: number };
};
export type Send = (config: HTTPRequest) => Promise<{ data: unknown }>;
export type UploadProgress = {
    stage: 'preparing' | 'uploading' | 'accepting' | 'accepted';
    transferredBytes: number;
    confirmedBytes: number;
    totalBytes: number;
    completedFiles: number;
    totalFiles: number;
};
type StartResponse = {
    upload_id: string; chunk_size: number; parallel_uploads: number;
    max_files_per_batch: number; max_upload_bytes: number; max_upload_files: number;
};
type Session = {
    id: string;
    files: readonly File[];
    prefix: string;
    entries: { path: string; size: number }[];
    totalBytes: number;
    offsets: number[];
    registeredFiles: number;
    options?: StartResponse;
    requestID?: string;
};
const retryDelays = [250, 1000, 2000];

function uuid(): string {
    // getRandomValues is also available when CVAT is served over a local HTTP connection.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] % 16) + 64;
    bytes[8] = (bytes[8] % 64) + 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function createSession(files: readonly File[], prefix: string): Session {
    if (!prefix.trim() || new TextEncoder().encode(prefix).length > 256) {
        throw new Error('接頭辞を256バイト以内で指定してください。');
    }
    if (!files.length) throw new Error('ファイルを選択してください。');
    const seen = new Set<string>();
    let totalBytes = 0;
    const entries = files.map((file) => {
        const path = safePath(file.webkitRelativePath || file.name);
        if (seen.has(path.toLowerCase())) throw new Error('重複するファイル名があります。');
        seen.add(path.toLowerCase());
        if (!Number.isSafeInteger(file.size) || file.size < 0) throw new Error('ファイルサイズが不正です。');
        totalBytes += file.size;
        if (!Number.isSafeInteger(totalBytes)) throw new Error('合計ファイルサイズが大きすぎます。');
        return { path, size: file.size };
    });
    return {
        id: uuid(),
        files: [...files],
        prefix,
        entries,
        totalBytes,
        offsets: files.map(() => 0),
        registeredFiles: 0,
    };
}

function transient(error: unknown): boolean {
    const response = (error as { response?: { status?: number } })?.response;
    return !response || [408, 425, 429, 500, 502, 503, 504].includes(response.status || 0);
}

function errorMessage(error: unknown, accepting: boolean): string {
    const response = (error as { response?: { data?: unknown; status?: number } })?.response;
    if (response?.status === 404 || response?.status === 410) {
        return '転送の保存期間が切れたか、プロジェクトにアクセスできません。画面を開き直してください。';
    }
    if (response?.status === 401 || response?.status === 403) return 'DICOMを取り込む権限を確認してください。';
    const data = response?.data;
    const detail = typeof data === 'string' ? data : (data as { detail?: unknown })?.detail;
    if (typeof detail === 'string' && detail.length < 1000) return detail;
    return accepting ?
        '登録受付を確認できませんでした。「受付を再確認」で同じ取り込みの結果を確認できます。' :
        '転送が中断しました。「転送を再開」で受け取り済みのデータから続行できます。';
}

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
        let timer: ReturnType<typeof setTimeout>;
        const abort = (): void => {
            clearTimeout(timer);
            reject(new Error('操作を中止しました。'));
        };
        timer = setTimeout(() => {
            signal.removeEventListener('abort', abort);
            resolve();
        }, milliseconds);
        signal.addEventListener('abort', abort, { once: true });
        if (signal.aborted) abort();
    });
}

export function createAPI(send: Send, base: string, projectID: number, org: string,
    identity: () => string): {
    available: () => Promise<void>;
    start: (files: readonly File[], prefix: string, progress?: (value: UploadProgress) => void) => Promise<string>;
    dispose: () => void; signal: AbortSignal;
} {
    const controller = new AbortController();
    const initial = identity();
    const url = `${base}/projects/${projectID}/dataset`;
    let session: Session | undefined;
    let running = false;
    const check = (signal = controller.signal): void => {
        if (!initial || identity() !== initial) {
            controller.abort();
            throw new Error('アカウントまたは組織が変更されました。');
        }
        if (signal.aborted || controller.signal.aborted) throw new Error('操作を中止しました。');
    };
    const request = async (config: Omit<HTTPRequest, 'params' | 'signal' | 'axios-retry'>,
        params: Record<string, string> = {}, signal = controller.signal): Promise<unknown> => {
        check(signal);
        const response = await send({
            ...config,
            params: { ...params, org },
            signal,
            'axios-retry': { retries: 0 },
        });
        check(signal);
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
        signal: controller.signal,
        available,
        dispose: () => {
            const current = session;
            const cancel = !controller.signal.aborted && initial && identity() === initial &&
                current && !current.requestID;
            controller.abort();
            if (cancel && current) {
                // An independent signal lets an explicit close release staged files after aborting transfer.
                // The server refuses to remove a session that has already entered the import queue.
                Promise.resolve().then(async () => {
                    if (identity() !== initial) return;
                    await send({
                        method: 'POST',
                        url,
                        data: { upload_id: current.id },
                        params: {
                            format: 'DICOM 1.0', location: 'local', dicom_upload: 'cancel', org,
                        },
                        headers: { 'Content-Type': 'application/json' },
                        signal: new AbortController().signal,
                        timeout: 15000,
                        'axios-retry': { retries: 0 },
                    });
                }).catch(() => {});
            }
        },
        start: async (files, prefix, progress = () => {}): Promise<string> => {
            check();
            if (running) throw new Error('転送処理が進行中です。');
            if (!session) session = createSession(files, prefix);
            const current = session;
            if (prefix !== current.prefix || files.length !== current.files.length ||
                files.some((file, index) => file !== current.files[index])) {
                throw new Error('転送を再開する場合は同じファイルと接頭辞を使用してください。');
            }
            running = true;
            const attempt = new AbortController();
            const abort = (): void => attempt.abort();
            controller.signal.addEventListener('abort', abort, { once: true });
            let stage: UploadProgress['stage'] = 'preparing';
            const inFlight = new Map<number, number>();
            // Update counters per acknowledged chunk, rather than rescanning a large folder on every event.
            let confirmedBytes = current.offsets.reduce((total, value) => total + value, 0);
            let completedFiles = current.offsets.filter((value, index) => value === files[index].size).length;
            // A failed receipt lookup must not turn completed transfer back into an upload state.
            if (current.registeredFiles === files.length && completedFiles === files.length) stage = 'accepting';
            const report = (): void => progress({
                stage,
                transferredBytes: confirmedBytes + Array.from(inFlight.values()).reduce(
                    (total, value) => total + value, 0,
                ),
                confirmedBytes,
                totalBytes: current.totalBytes,
                completedFiles,
                totalFiles: files.length,
            });
            const operation = async (name: string, data: Blob | Record<string, unknown>,
                params: Record<string, string> = {}, onUploadProgress?: HTTPRequest['onUploadProgress']): Promise<unknown> => {
                for (let retry = 0; ; retry++) {
                    try {
                        return await request({
                            method: 'POST',
                            url,
                            data,
                            timeout: 0,
                            onUploadProgress,
                            headers: { 'Content-Type': name === 'chunk' ? 'application/octet-stream' : 'application/json' },
                        }, {
                            format: 'DICOM 1.0', location: 'local', dicom_upload: name, ...params,
                        }, attempt.signal);
                    } catch (error) {
                        check(attempt.signal);
                        if (retry >= retryDelays.length || !transient(error)) throw error;
                        // Every operation is scoped to one upload ID and is idempotent on the server.
                        onUploadProgress?.({ loaded: 0 });
                        await pause(retryDelays[retry], attempt.signal);
                    }
                }
            };
            try {
                report();
                if (!current.options) {
                    const options = await operation('start', {
                        upload_id: current.id, prefix, file_count: files.length, total_bytes: current.totalBytes,
                    }) as StartResponse;
                    if (options?.upload_id !== current.id ||
                        ![options.chunk_size, options.parallel_uploads, options.max_files_per_batch,
                            options.max_upload_bytes, options.max_upload_files].every(
                            (value) => Number.isSafeInteger(value) && value > 0,
                        ) ||
                        options.chunk_size > 64 * 1024 ** 2 || options.max_files_per_batch > 500 ||
                        current.totalBytes > options.max_upload_bytes || files.length > options.max_upload_files) {
                        throw new Error('サーバーの転送設定を確認できませんでした。');
                    }
                    current.options = options;
                } else if (!current.requestID) {
                    const state = await operation('status', { upload_id: current.id }) as {
                        registered_files: number; rq_id?: string;
                    };
                    if (!Number.isSafeInteger(state?.registered_files) || state.registered_files < 0 ||
                        state.registered_files > files.length) throw new Error('Invalid upload status');
                    current.registeredFiles = state.registered_files;
                    if (typeof state.rq_id === 'string' && state.rq_id) current.requestID = state.rq_id;
                }
                const { options } = current;
                while (!current.requestID && current.registeredFiles < files.length) {
                    const result = await operation('files', {
                        upload_id: current.id,
                        start_index: current.registeredFiles,
                        files: current.entries.slice(
                            current.registeredFiles, current.registeredFiles + options.max_files_per_batch,
                        ),
                    }) as { registered_files: number };
                    if (!Number.isSafeInteger(result?.registered_files) ||
                        result.registered_files <= current.registeredFiles || result.registered_files > files.length) {
                        throw new Error('Invalid file registration response');
                    }
                    current.registeredFiles = result.registered_files;
                }
                if (!current.requestID) {
                    stage = 'uploading';
                    report();
                    let nextFile = 0;
                    let failure: unknown;
                    const worker = async (): Promise<void> => {
                        try {
                            while (nextFile < files.length) {
                                check(attempt.signal);
                                const index = nextFile++;
                                const file = files[index];
                                while (current.offsets[index] < file.size) {
                                    const offset = current.offsets[index];
                                    const end = Math.min(offset + options.chunk_size, file.size);
                                    const result = await operation('chunk', file.slice(offset, end), {
                                        upload_id: current.id, file_index: String(index), offset: String(offset),
                                    }, (event) => {
                                        if (attempt.signal.aborted || controller.signal.aborted) return;
                                        if (identity() !== initial) { controller.abort(); return; }
                                        inFlight.set(index, Number.isFinite(event.loaded) ?
                                            Math.max(0, Math.min(event.loaded, end - offset)) : 0);
                                        report();
                                    }) as { offset: number };
                                    if (result?.offset !== end) throw new Error('Invalid upload offset');
                                    current.offsets[index] = end;
                                    confirmedBytes += end - offset;
                                    inFlight.delete(index);
                                    if (end === file.size) completedFiles++;
                                    report();
                                }
                            }
                        } catch (error) {
                            if (!failure) failure = error;
                            attempt.abort();
                        }
                    };
                    const workers = Math.min(options.parallel_uploads, 4, files.length);
                    await Promise.all(Array.from({ length: workers }, worker));
                    if (failure) throw failure;
                    check(attempt.signal);
                    stage = 'accepting';
                    report();
                    const result = await operation('finish', { upload_id: current.id }) as { rq_id?: string };
                    if (typeof result?.rq_id !== 'string' || !result.rq_id) throw new Error('Invalid request response');
                    current.requestID = result.rq_id;
                }
                stage = 'accepted';
                confirmedBytes = current.totalBytes;
                completedFiles = files.length;
                inFlight.clear();
                report();
                return current.requestID;
            } catch (error) {
                check();
                inFlight.clear();
                report();
                throw new Error(errorMessage(error, stage === 'accepting'));
            } finally {
                controller.signal.removeEventListener('abort', abort);
                attempt.abort();
                running = false;
            }
        },
    };
}
export type API = ReturnType<typeof createAPI>;
