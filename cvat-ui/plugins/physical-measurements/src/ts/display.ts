// SPDX-License-Identifier: MIT
import {
    DicomTagState, PixelSpacing, formatLength, polylineLength, resolveSpacing,
} from './measurement';

export interface Shape {
    shapeType: string;
    points: readonly number[];
    frame: number;
}
export interface MeasurementCanvas {
    configure(options: { shapeText: ((shape: Shape) => string | null) | null }): void;
}
export interface DisplayTarget {
    key: string;
    canvas: MeasurementCanvas;
    annotations: {
        get(frame: number, allTracks: boolean, filters: object[]): Promise<readonly DicomTagState[]>;
    };
    frame: number;
    revision: unknown;
}

export function shapeText(frame: number, spacing: PixelSpacing | null): (shape: Shape) => string | null {
    return (shape) => {
        if (shape.shapeType !== 'polyline' || shape.frame !== frame ||
            !Number.isSafeInteger(frame) || frame < 0) return null;
        return formatLength(polylineLength(shape.points, spacing), spacing ? 'mm' : 'px');
    };
}

export function createDisplay(failed: (error: unknown) => void): {
    update(target: DisplayTarget | null): void;
    dispose(): void;
} {
    let target: DisplayTarget | null = null;
    let generation = 0;
    const update = (next: DisplayTarget | null): void => {
        if (next?.key === target?.key && next?.canvas === target?.canvas &&
            next?.annotations === target?.annotations && next?.frame === target?.frame &&
            next?.revision === target?.revision) return;
        const request = ++generation;
        if (target) target.canvas.configure({ shapeText: null });
        target = next;
        if (!next) return;
        if (!Number.isSafeInteger(next.frame) || next.frame < 0) return;
        // get() reads CVAT's shared collection, including unsaved changes. Empty
        // filters keep metadata available when the tag is hidden by a UI filter.
        next.annotations.get(next.frame, false, []).then((states) => {
            if (target === next && request === generation) {
                next.canvas.configure({ shapeText: shapeText(next.frame, resolveSpacing(states, next.frame)) });
            }
        }).catch((error: unknown) => {
            if (target === next && request === generation) failed(error);
        });
    };
    return { update, dispose: () => update(null) };
}
