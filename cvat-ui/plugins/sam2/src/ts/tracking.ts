// SPDX-License-Identifier: MIT
// Pure orchestration, isolated from CVAT classes for deterministic contract tests.
import { MAX_TRACKING_OBJECTS, MAX_TRACKING_FRAMES } from './selection';

export interface Polygon { type: 'polygon'; points: number[]; }
export interface Mask { type: 'mask'; points: number[]; }
export type Seed = Polygon | Mask;
export interface Keyframe { frame: number; points: number[]; outside: boolean; }
export interface FrameMeta { width: number; height: number; deleted: boolean; }
export interface TrackerReply { states: string[]; shapes: (Polygon | null)[]; }
export interface CallArgs { frame: number; shapes?: Seed[]; states?: string[]; }
export interface Transport {
    call(args: CallArgs): Promise<TrackerReply>;
    frame(number: number): Promise<FrameMeta>;
    wait?(milliseconds: number): Promise<void>; // Test seam for retry delays.
}
export interface TrackingInput {
    trackerName?: string;
    seeds: Seed[];
    first?: number;
    direction?: 1 | -1;
    start: number;
    stop: number;
    span: number;
    frameNumbers: number[];
    width: number;
    height: number;
}

function validateReply(reply: TrackerReply, count: number, width: number, height: number, name: string): void {
    if (!reply || !Array.isArray(reply.states) || !Array.isArray(reply.shapes) ||
        reply.states.length !== count || reply.shapes.length !== count ||
        reply.states.some((state) => typeof state !== 'string' || !state.length)) {
        throw new Error(`${name}の追跡結果を読み取れません。結果は反映していません。実行し直しても改善しない場合は管理者に連絡してください。`);
    }
    for (const shape of reply.shapes) {
        if (shape === null) continue;
        if (!shape || shape.type !== 'polygon' || !Array.isArray(shape.points) ||
            shape.points.length < 6 || shape.points.length % 2 || shape.points.length > 20000 ||
            shape.points.some((point, index) => !Number.isFinite(point) || point < 0 ||
                point > (index % 2 ? height : width))) {
            throw new Error(`${name}が返したポリゴンの形状が不正です。結果は反映していません。管理者にモデルの確認を依頼してください。`);
        }
    }
}

// Retry continuation requests only. Reuse the exact previous signed states and frame.
// The Redis CAS path returns the saved response if the first request committed but
// its response was lost. Initialization is not idempotent and is deliberately not retried.
export async function continueWithRetry(
    args: CallArgs, transport: Transport, cancelled: () => boolean,
): Promise<TrackerReply | null> {
    for (let attempt = 0; ; attempt++) {
        if (cancelled()) return null;
        try {
            return await transport.call(args);
        } catch (error) {
            const code = (error as { code?: number })?.code;
            if (attempt >= 2 || typeof code !== 'number' || ![0, 500, 502, 503, 504].includes(code)) throw error;
            // Poll cancellation during backoff. No new HTTP request after cancellation.
            for (let tick = 0; tick < (attempt + 1) * 10; tick++) {
                if (cancelled()) return null;
                await (transport.wait ? transport.wait(100) : new Promise((resolve) => { setTimeout(resolve, 100); }));
            }
        }
    }
}

export async function track(
    input: TrackingInput,
    transport: Transport,
    cancelled: () => boolean,
    progress: (message: string, percent: number) => void,
): Promise<Keyframe[][] | null> {
    const {
        seeds, start, stop, span, width, height, first = Math.min(...input.frameNumbers), direction = 1, trackerName = 'SAM2',
    } = input;
    if (direction !== 1 && direction !== -1) throw new Error('追跡方向を選択し直してください。');
    if (!seeds.length || seeds.length > MAX_TRACKING_OBJECTS) {
        throw new Error(`開始フレームのポリゴンまたはマスクを1個から${MAX_TRACKING_OBJECTS}個まで選択してください。`);
    }
    if (!Number.isInteger(span) || span < 1 || span > MAX_TRACKING_FRAMES) {
        throw new Error(`開始フレームと終了フレームの差は1から${MAX_TRACKING_FRAMES}の範囲で指定してください。`);
    }
    const frameNumbers = [...new Set(input.frameNumbers)].sort((a, b) => a - b);
    if (!frameNumbers.includes(start)) throw new Error('開始フレームがこのジョブにありません。ジョブ内のフレームを選択してください。');
    const end = direction === 1 ? Math.min(stop, start + span) : Math.max(first, start - span);
    const targets = frameNumbers.filter((frame) => (frame - start) * direction > 0 && (end - frame) * direction >= 0);
    if (direction === -1) targets.reverse();
    if (!targets.length) throw new Error('選択した方向に追跡できるフレームがありません。開始フレームまたは追跡方向を変更してください。');
    if (cancelled()) return null;
    const seedMeta = await transport.frame(start);
    if (seedMeta.deleted || seedMeta.width !== width || seedMeta.height !== height) {
        throw new Error('開始フレームが削除されたか、画像サイズが変更されました。開始フレームを選択し直してください。');
    }
    if (cancelled()) return null;
    let response = await transport.call({ frame: start, shapes: seeds });
    if (cancelled()) return null;
    validateReply(response, seeds.length, width, height, trackerName);
    // Preserve the user's exact seed contour, not the model's reconstruction of it.
    const results = seeds.map((seed, index) => {
        const points = seed.type === 'polygon' ? seed.points : response.shapes[index]?.points;
        if (!points) throw new Error('開始フレームのマスクからポリゴンを作成できません。マスクを確認するか、ポリゴンで対象を指定してください。');
        return [{ frame: start, points: [...points], outside: false }];
    });
    let processed = 0;
    let coordinateCount = results.reduce((sum, result) => sum + result[0].points.length, 0);
    for (let i = 0; i < targets.length; i++) {
        const frame = targets[i];
        if (cancelled()) return null;
        const meta = await transport.frame(frame);
        if (cancelled()) return null;
        if (!meta.deleted) {
            if (meta.width !== width || meta.height !== height) throw new Error('画像サイズが異なるフレームが含まれています。同じ画像サイズの範囲を指定してください。');
            const continued = await continueWithRetry({ frame, states: response.states }, transport, cancelled);
            if (!continued) return null;
            response = continued;
            if (cancelled()) return null;
            validateReply(response, seeds.length, width, height, trackerName);
            for (let index = 0; index < response.shapes.length; index++) {
                const shape = response.shapes[index];
                const previous = results[index][results[index].length - 1];
                const points = shape?.points ?? previous.points;
                coordinateCount += points.length;
                if (coordinateCount > 2_000_000) throw new Error('追跡結果のサイズが上限を超えました。追跡範囲を短くするか、対象数を減らしてください。');
                results[index].push({ frame, points: [...points], outside: shape === null });
            }
            processed += 1;
        }
        progress(`${trackerName}: フレーム ${frame} を追跡しています。`, Math.floor(((i + 1) / targets.length) * 99));
    }
    if (!processed) throw new Error('選択した範囲の対象フレームはすべて削除されています。別の範囲を指定してください。');
    // CVAT extrapolates the last visible keyframe. Explicitly terminate the new track
    // at the first available, non-deleted frame beyond the requested range.
    const beyond = frameNumbers.filter((number) => (number - end) * direction > 0 && number >= first && number <= stop);
    if (direction === -1) beyond.reverse();
    for (const frame of beyond) {
        if (cancelled()) return null;
        const meta = await transport.frame(frame);
        if (cancelled()) return null;
        if (meta.deleted) continue;
        results.forEach((keyframes) => {
            const last = keyframes[keyframes.length - 1];
            keyframes.push({ frame, points: [...last.points], outside: true });
        });
        break;
    }
    if (direction === -1) {
        // Close at both sides: CVAT extrapolates the latest visible keyframe.
        for (const frame of frameNumbers.filter((number) => number > start && number <= stop)) {
            if (cancelled()) return null;
            if ((await transport.frame(frame)).deleted) continue;
            results.forEach((keyframes) => keyframes.push({ frame, points: [...keyframes[0].points], outside: true }));
            break;
        }
    }
    return cancelled() ? null : results.map((keyframes) => keyframes.sort((a, b) => a.frame - b.frame));
}
