// SPDX-License-Identifier: MIT
import type { MeasurementAPI, MeasurementDocument } from './api';
import { formatLength, polylineLength, resolveSpacing } from './measurement';

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
    api: MeasurementAPI;
}

export function shapeText(document: MeasurementDocument): (shape: Shape) => string | null {
    return (shape) => {
        if (!document.supported || shape.shapeType !== 'polyline' ||
            !Number.isSafeInteger(shape.frame) || shape.frame < 0 || shape.frame >= document.frame_count) return null;
        const spacing = resolveSpacing(document, shape.frame);
        return formatLength(polylineLength(shape.points, spacing), spacing ? 'mm' : 'px');
    };
}

export function createDisplay(failed: (error: unknown) => void): {
    update(target: DisplayTarget | null): void;
    refresh(): Promise<void>;
    dispose(): void;
} {
    let target: DisplayTarget | null = null;
    let generation = 0;
    const refresh = async (): Promise<void> => {
        const current = target;
        const request = ++generation;
        if (!current) return;
        // No calibration from an earlier job or revision remains visible during a reload.
        current.canvas.configure({ shapeText: null });
        try {
            const document = await current.api.get();
            if (target === current && generation === request) {
                current.canvas.configure({ shapeText: document.supported ? shapeText(document) : null });
            }
        } catch (error) {
            if (target === current && generation === request) failed(error);
        }
    };
    const update = (next: DisplayTarget | null): void => {
        if (next?.key === target?.key && next?.canvas === target?.canvas) return;
        generation++;
        if (target) {
            target.canvas.configure({ shapeText: null });
            target.api.dispose();
        }
        target = next;
        if (next) refresh();
    };
    return { update, refresh, dispose: () => update(null) };
}
