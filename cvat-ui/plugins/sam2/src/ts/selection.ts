// SPDX-License-Identifier: MIT
/** Shared UI/orchestration limits; server-side tensor/response byte limits still apply. */
export const MAX_TRACKING_OBJECTS = 16;
export const MAX_TRACKING_FRAMES = 10_000;

/** Pure range/selection rules shared by the dialog and its CPU-only tests. */
export function validateRange(start: number, end: number, first: number, last: number): string | null {
    if (!Number.isInteger(start) || !Number.isInteger(end)) return 'フレーム番号は整数で指定してください。';
    if (Math.min(start, end) < first || Math.max(start, end) > last) return 'ジョブの範囲内を指定してください。';
    if (end === start) return '開始と終了に異なるフレームを指定してください。';
    if (Math.abs(end - start) > MAX_TRACKING_FRAMES) return `開始フレームと終了フレームの差は${MAX_TRACKING_FRAMES}以内にしてください。`;
    return null;
}

export function resolveSelection<T extends { clientID: number }>(candidates: T[], ids: number[]): T[] {
    if (!ids.length || ids.length > MAX_TRACKING_OBJECTS || new Set(ids).size !== ids.length) {
        throw new Error(`開始フレームのポリゴンまたはマスクを1個から${MAX_TRACKING_OBJECTS}個まで選択してください。`);
    }
    return ids.map((id) => {
        const state = candidates.find((candidate) => candidate.clientID === id);
        if (!state) throw new Error('選択した対象がありません。開始フレームを確認してください。');
        return state;
    });
}
