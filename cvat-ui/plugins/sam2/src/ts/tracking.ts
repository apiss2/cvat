// SPDX-License-Identifier: MIT
// Pure orchestration, isolated from CVAT classes for deterministic contract tests.
export interface Polygon { type: 'polygon'; points: number[]; }
export interface Keyframe { frame: number; points: number[]; outside: boolean; }
export interface FrameMeta { width: number; height: number; deleted: boolean; }
export interface TrackerReply { states: string[]; shapes: (Polygon | null)[]; }
export interface CallArgs { frame: number; shapes?: Polygon[]; states?: string[]; }
export interface Transport {
    call(args: CallArgs): Promise<TrackerReply>;
    frame(number: number): Promise<FrameMeta>;
    wait?(milliseconds: number): Promise<void>; // Test seam for retry delays.
}
export interface TrackingInput {
    seeds: Polygon[];
    start: number;
    stop: number;
    span: number;
    frameNumbers: number[];
    width: number;
    height: number;
}

function validateReply(reply: TrackerReply, count: number, width: number, height: number): void {
    if (!reply || !Array.isArray(reply.states) || !Array.isArray(reply.shapes) ||
        reply.states.length !== count || reply.shapes.length !== count ||
        reply.states.some((state) => typeof state !== 'string' || !state.length)) {
        throw new Error('追跡結果を読み込めませんでした。結果は適用されていません。追跡をやり直してください。');
    }
    for (const shape of reply.shapes) {
        if (shape === null) continue;
        if (!shape || shape.type !== 'polygon' || !Array.isArray(shape.points) ||
            shape.points.length < 6 || shape.points.length % 2 || shape.points.length > 20000 ||
            shape.points.some((point, index) => !Number.isFinite(point) || point < 0 ||
                point > (index % 2 ? height : width))) {
            throw new Error('追跡結果に不正な輪郭が含まれています。結果は適用されていません。');
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
                await (transport.wait ? transport.wait(100) : new Promise((resolve) => setTimeout(resolve, 100)));
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
    const { seeds, start, stop, span, width, height } = input;
    if (!seeds.length || seeds.length > 4) throw new Error('追跡するポリゴンを1個から4個選択してください。');
    if (!Number.isInteger(span) || span < 1 || span > 1000) throw new Error('追跡範囲は、開始位置の1フレーム先から1000フレーム先までの間で指定してください。');
    const frameNumbers = [...new Set(input.frameNumbers)].sort((a, b) => a - b);
    if (!frameNumbers.includes(start)) throw new Error('このジョブ内の画像で、追跡を開始するポリゴンを選択してください。');
    const end = Math.min(stop, start + span);
    const targets = frameNumbers.filter((frame) => frame > start && frame <= end);
    if (!targets.length) throw new Error('選択した範囲に追跡先の画像がありません。開始位置か追跡範囲を変更してください。');
    if (cancelled()) return null;
    const seedMeta = await transport.frame(start);
    if (seedMeta.deleted || seedMeta.width !== width || seedMeta.height !== height) {
        throw new Error('開始画像が削除されたか、画像の大きさが変更されました。画面を再読み込みして対象を選び直してください。');
    }
    if (cancelled()) return null;
    let response = await transport.call({ frame: start, shapes: seeds });
    if (cancelled()) return null;
    validateReply(response, seeds.length, width, height);
    // Preserve the user's exact seed contour, not the model's reconstruction of it.
    const results = seeds.map((seed) => [{ frame: start, points: [...seed.points], outside: false }]);
    let processed = 0;
    let coordinateCount = seeds.reduce((sum, seed) => sum + seed.points.length, 0);
    for (let i = 0; i < targets.length; i++) {
        const frame = targets[i];
        if (cancelled()) return null;
        const meta = await transport.frame(frame);
        if (cancelled()) return null;
        if (!meta.deleted) {
            if (meta.width !== width || meta.height !== height) throw new Error('画像の大きさが途中で変わるため、追跡を中止しました。結果は適用されていません。');
            const continued = await continueWithRetry({ frame, states: response.states }, transport, cancelled);
            if (!continued) return null;
            response = continued;
            if (cancelled()) return null;
            validateReply(response, seeds.length, width, height);
            response.shapes.forEach((shape, index) => {
                const previous = results[index][results[index].length - 1];
                const points = shape?.points ?? previous.points;
                coordinateCount += points.length;
                if (coordinateCount > 2_000_000) throw new Error('追跡結果が大きすぎるため、適用できません。追跡範囲を短くしてください。');
                results[index].push({ frame, points: [...points], outside: shape === null });
            });
            processed += 1;
        }
        progress(`SAM2で追跡中: フレーム ${frame}`, Math.floor(((i + 1) / targets.length) * 99));
    }
    if (!processed) throw new Error('選択した範囲の追跡先画像はすべて削除されています。追跡範囲を変更してください。');
    // CVAT extrapolates the last visible keyframe. Explicitly terminate the new track
    // at the first available, non-deleted frame beyond the requested range.
    for (const frame of frameNumbers.filter((number) => number > end && number <= stop)) {
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
    return cancelled() ? null : results;
}
