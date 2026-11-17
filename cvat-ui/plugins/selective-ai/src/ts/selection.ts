// SPDX-License-Identifier: MIT
export interface Condition { taskFilter: object; jobFilter: object; filters: object[] }
export interface FrameMeta { size: number; deleted_frames?: number[] }
export interface JobInfo { id: number; task_id: number; type: string; start_frame: number; stop_frame: number; frame_count: number }
export interface AnnotationSession {
    size: number;
    annotations: {
        clear(options: { reload: boolean }): Promise<void>;
        search(from: number, to: number, options: {
            allowDeletedFrames: boolean; annotationsFilters: object[];
        }): Promise<number | null>;
    };
}

export class CancelledError extends Error {
    constructor() { super('処理を中止しました。'); }
}

export const hasRule = (rule: object): boolean => Object.keys(rule).length > 0;
const isRule = (rule: unknown): rule is object => !!rule && typeof rule === 'object' && !Array.isArray(rule);

export function validateCondition(condition: Condition): void {
    if (!condition || !isRule(condition.taskFilter) || !isRule(condition.jobFilter) ||
        !Array.isArray(condition.filters) || condition.filters.length > 2 || !condition.filters.every(isRule)) {
        throw new Error('フィルターを確認してください。');
    }
}

export function taskFilterForID(rule: object, id: number): string {
    return JSON.stringify({ and: [{ '==': [{ var: 'id' }, id] }, rule] });
}

function frameInfo(meta: FrameMeta): { valid: (frame: number) => boolean; deleted: Set<number> } {
    if (!Number.isSafeInteger(meta.size) || meta.size < 0) {
        throw new Error('フレーム情報が不正です。');
    }
    const valid = (frame: number): boolean => Number.isSafeInteger(frame) && frame >= 0 && frame < meta.size;
    if (!Array.isArray(meta.deleted_frames ?? []) || !(meta.deleted_frames ?? []).every(valid)) {
        throw new Error('削除フレーム情報が不正です。');
    }
    return { valid, deleted: new Set(meta.deleted_frames ?? []) };
}

export function allFrames(meta: FrameMeta): number[] {
    const { deleted } = frameInfo(meta);
    return Array.from({ length: meta.size }, (_, frame) => frame).filter((frame) => !deleted.has(frame));
}

// Ordinary annotation jobs use inclusive, task-relative start/stop positions.
// The range union handles overlaps without converting source-video frame numbers.
export function selectFrames(meta: FrameMeta, jobs: JobInfo[], taskId: number): number[] {
    const { valid, deleted } = frameInfo(meta);
    if (!Array.isArray(jobs)) throw new Error('Job一覧が不正です。');
    const selected = new Set<number>();
    for (const job of jobs) {
        if (job.task_id !== taskId || job.type !== 'annotation' || !valid(job.start_frame) ||
            !valid(job.stop_frame) || job.start_frame > job.stop_frame ||
            job.frame_count !== job.stop_frame - job.start_frame + 1) {
            throw new Error('Jobの範囲または所属が不正です。');
        }
        for (let frame = job.start_frame; frame <= job.stop_frame; frame++) {
            if (!deleted.has(frame)) selected.add(frame);
        }
    }
    return [...selected].sort((a, b) => a - b);
}

// Reuse the annotation navigation search, never Job.annotations.get (which may
// be intercepted by frame-navigation-filter). Each contiguous candidate range
// is searched independently, so a match cannot escape the selected Job ranges.
export async function searchFrames(session: AnnotationSession, candidates: number[], filters: object[],
    check: () => void): Promise<number[]> {
    check();
    if (!candidates.length || !filters.some(hasRule)) return [...candidates];
    if (candidates.some((frame, index) => !Number.isSafeInteger(frame) || frame < 0 ||
        frame >= session.size || (index > 0 && frame <= candidates[index - 1]))) {
        throw new Error('画像の選択範囲が不正です。');
    }
    await session.annotations.clear({ reload: true });
    check();
    const matches: number[] = [];
    for (let i = 0; i < candidates.length;) {
        let from = candidates[i];
        while (i + 1 < candidates.length && candidates[i + 1] === candidates[i] + 1) i++;
        const to = candidates[i++];
        while (from <= to) {
            check();
            // Fresh task metadata already excluded deleted frames.
            const match = await session.annotations.search(from, to, {
                allowDeletedFrames: true, annotationsFilters: filters,
            });
            check();
            if (match === null) break;
            if (!Number.isSafeInteger(match) || match < from || match > to) {
                throw new Error('画像フィルターの検索結果が不正です。');
            }
            matches.push(match);
            from = match + 1;
        }
    }
    return matches;
}
