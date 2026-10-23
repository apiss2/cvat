// SPDX-License-Identifier: MIT
import { MeasurementConfig, parseMeasurementConfig } from './measurement';

export interface MeasurementDocument extends MeasurementConfig {
    revision: number;
    supported: boolean;
    unavailable_reason: string | null;
    frame_count: number;
    readOnly?: boolean;
}

export interface MeasurementAPI {
    get(): Promise<MeasurementDocument>;
    save(config: MeasurementConfig, expectedRevision: number): Promise<MeasurementDocument>;
    dispose(): void;
}

export interface Request {
    method: 'GET' | 'PUT';
    url: string;
    params: { org: string };
    data?: unknown;
    timeout: number;
    signal: AbortSignal;
}
export type Transport = (request: Request) => Promise<{ data: unknown }>;

function parseDocument(value: unknown): MeasurementDocument {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('spacingの応答形式が不正です。');
    }
    const data = value as Record<string, unknown>;
    const config = parseMeasurementConfig({ version: data.version, default: data.default, frames: data.frames });
    if (!Number.isSafeInteger(data.revision) || (data.revision as number) < 0 ||
        !Number.isSafeInteger(data.frame_count) || (data.frame_count as number) < 0 ||
        typeof data.supported !== 'boolean' ||
        !(data.unavailable_reason === null || typeof data.unavailable_reason === 'string') ||
        Object.keys(config.frames).some((frame) => Number(frame) >= (data.frame_count as number))) {
        throw new Error('spacingの応答形式が不正です。');
    }
    return {
        ...config,
        revision: data.revision as number,
        supported: data.supported,
        unavailable_reason: data.unavailable_reason as string | null,
        frame_count: data.frame_count as number,
    };
}

export function createAPI(send: Transport, base: string, type: 'task' | 'job', id: number,
    org: string, identity: () => string): MeasurementAPI {
    if (!Number.isSafeInteger(id) || id <= 0 || !['task', 'job'].includes(type)) {
        throw new Error('対象のIDが不正です。');
    }
    const owner = identity();
    const controller = new AbortController();
    let invalidated = false;
    const guard = (): void => {
        invalidated = invalidated || !owner || identity() !== owner || controller.signal.aborted;
        if (invalidated) throw new Error('利用者または組織が変更されました。画面を開き直してください。');
    };
    const request = async (data?: unknown): Promise<MeasurementDocument> => {
        guard();
        let response: { data: unknown };
        try {
            response = await send({
                method: data ? 'PUT' : 'GET',
                url: `${base.replace(/\/$/, '')}/physical-measurements/${type}s/${id}`,
                params: { org },
                data,
                timeout: 30000,
                signal: controller.signal,
            });
        } catch (error) {
            guard();
            throw error;
        }
        guard();
        return { ...parseDocument(response.data), readOnly: type === 'job' };
    };
    return {
        get: () => request(),
        save: (config, expectedRevision) => {
            guard();
            if (type !== 'task') throw new Error('この画面ではspacingを変更できません。');
            if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
                throw new Error('spacingを読み込み直してください。');
            }
            const parsed = parseMeasurementConfig(config);
            return request({ ...parsed, expected_revision: expectedRevision });
        },
        dispose: () => { invalidated = true; controller.abort(); },
    };
}

// A job assignee can inspect calibration without gaining permission to edit the task.
export function createEditorAPI(task: MeasurementAPI, job?: MeasurementAPI): MeasurementAPI {
    let readOnly = false;
    return {
        get: async () => {
            try {
                const document = await task.get();
                readOnly = false;
                return document;
            } catch (error) {
                if (!job || (error as { response?: { status?: number } })?.response?.status !== 403) throw error;
                readOnly = true;
                return { ...await job.get(), readOnly: true };
            }
        },
        save: (config, revision) => {
            if (readOnly) throw new Error('この画面ではspacingを変更できません。');
            return task.save(config, revision);
        },
        dispose: () => { task.dispose(); job?.dispose(); },
    };
}

export function errorMessage(error: unknown): string {
    const response = (error as { response?: { status?: number; data?: unknown } } | null)?.response;
    if (response?.status === 409) return '他の操作でspacingが更新されました。「再読込」で最新の設定を確認してください。';
    if (response?.status === 403) return 'このタスクのspacingを変更する権限がありません。';
    const data = response?.data;
    if (data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string') return data.detail;
    if (typeof data === 'string') return data;
    return error instanceof Error ? error.message : String(error);
}
