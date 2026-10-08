// SPDX-License-Identifier: MIT
/** Pure range/selection rules shared by the dialog and its CPU-only tests. */
export function validateRange(start: number, end: number, first: number, last: number): string | null {
    if (!Number.isInteger(start) || !Number.isInteger(end)) return 'フレーム番号は整数で指定してください。';
    if (start < first || end > last) return 'ジョブの範囲内を指定してください。';
    if (end <= start) return '終了フレームは開始フレームより後を指定してください。';
    if (end - start > 1000) return '一度に指定できる範囲は開始から1000フレーム先までです。';
    return null;
}

export function resolveSelection<T extends { clientID: number }>(candidates: T[], ids: number[]): T[] {
    if (!ids.length || ids.length > 4 || new Set(ids).size !== ids.length) {
        throw new Error('初期ポリゴンを1個から4個まで選択してください。');
    }
    return ids.map((id) => {
        const state = candidates.find((candidate) => candidate.clientID === id);
        if (!state) throw new Error('選択したポリゴンがありません。開始フレームを確認してください。');
        return state;
    });
}
