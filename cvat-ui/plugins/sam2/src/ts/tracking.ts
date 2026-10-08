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
        throw new Error('Invalid SAM2 tracker response; signed states and shapes must match the seeds.');
    }
    for (const shape of reply.shapes) {
        if (shape === null) continue;
        if (!shape || shape.type !== 'polygon' || !Array.isArray(shape.points) ||
            shape.points.length < 6 || shape.points.length % 2 || shape.points.length > 20000 ||
            shape.points.some((point, index) => !Number.isFinite(point) || point < 0 ||
                point > (index % 2 ? height : width))) {
            throw new Error('SAM2 returned an invalid polygon.');
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
    if (!seeds.length || seeds.length > 4) throw new Error('Select between one and four polygon shapes.');
    if (!Number.isInteger(span) || span < 1 || span > 1000) throw new Error('Frames to track must be 1..1000.');
    const frameNumbers = [...new Set(input.frameNumbers)].sort((a, b) => a - b);
    if (!frameNumbers.includes(start)) throw new Error('The seed frame does not belong to this job.');
    const end = Math.min(stop, start + span);
    const targets = frameNumbers.filter((frame) => frame > start && frame <= end);
    if (!targets.length) throw new Error('There are no later frames in the selected range.');
    if (cancelled()) return null;
    const seedMeta = await transport.frame(start);
    if (seedMeta.deleted || seedMeta.width !== width || seedMeta.height !== height) {
        throw new Error('The seed frame is deleted or its dimensions changed.');
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
            if (meta.width !== width || meta.height !== height) throw new Error('Video frame dimensions changed.');
            const continued = await continueWithRetry({ frame, states: response.states }, transport, cancelled);
            if (!continued) return null;
            response = continued;
            if (cancelled()) return null;
            validateReply(response, seeds.length, width, height);
            response.shapes.forEach((shape, index) => {
                const previous = results[index][results[index].length - 1];
                const points = shape?.points ?? previous.points;
                coordinateCount += points.length;
                if (coordinateCount > 2_000_000) throw new Error('Tracking result is too large; choose a shorter range.');
                results[index].push({ frame, points: [...points], outside: shape === null });
            });
            processed += 1;
        }
        progress(`SAM2 tracking: frame ${frame}`, Math.floor(((i + 1) / targets.length) * 99));
    }
    if (!processed) throw new Error('All later frames in the selected range are deleted.');
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
