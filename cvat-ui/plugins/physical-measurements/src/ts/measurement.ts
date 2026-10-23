// SPDX-License-Identifier: MIT

export interface PixelSpacing {
    x: number;
    y: number;
    unit: 'mm';
}

export interface MeasurementConfig {
    version: 1;
    default: PixelSpacing | null;
    // Keys are task-relative frame numbers, including when a job starts after frame zero.
    frames: Record<string, PixelSpacing | null>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
}

function hasExactlyKeys(value: Record<string, unknown>, keys: string[]): boolean {
    const actual = Object.keys(value);
    return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function isSpacing(value: unknown): value is PixelSpacing {
    return isRecord(value) && hasExactlyKeys(value, ['x', 'y', 'unit']) &&
        typeof value.x === 'number' && Number.isFinite(value.x) && value.x > 0 &&
        typeof value.y === 'number' && Number.isFinite(value.y) && value.y > 0 &&
        value.unit === 'mm';
}

function parseSpacing(value: unknown, field: string): PixelSpacing | null {
    if (value === null) return null;
    if (!isSpacing(value)) {
        throw new TypeError(`${field} must be null or {x, y, unit: "mm"} with positive finite numbers`);
    }
    return { x: value.x, y: value.y, unit: 'mm' };
}

// API concurrency tokens are separate from this portable configuration. Reject
// unknown fields so misspelled calibration settings cannot silently become defaults.
export function parseMeasurementConfig(value: unknown): MeasurementConfig {
    if (!isRecord(value) || !hasExactlyKeys(value, ['version', 'default', 'frames'])) {
        throw new TypeError('Configuration must contain exactly version, default and frames');
    }
    if (value.version !== 1) throw new TypeError('version must be 1');
    if (!isRecord(value.frames)) throw new TypeError('frames must be an object');

    const defaultSpacing = parseSpacing(value.default, 'default');
    const frames: MeasurementConfig['frames'] = {};
    for (const [frame, spacing] of Object.entries(value.frames)) {
        if (!/^(0|[1-9]\d*)$/.test(frame) || !Number.isSafeInteger(Number(frame))) {
            throw new TypeError(`Invalid task frame number: ${frame}`);
        }
        frames[frame] = parseSpacing(spacing, `frames[${frame}]`);
    }

    return { version: 1, default: defaultSpacing, frames };
}

export function resolveSpacing(config: MeasurementConfig | null, frame: number): PixelSpacing | null {
    if (!config || config.version !== 1 || !isRecord(config.frames) ||
        !Number.isSafeInteger(frame) || frame < 0) return null;

    // An explicit null (or an invalid runtime override) disables measurement on
    // that frame. It must never fall back to a different calibration.
    const spacing = Object.prototype.hasOwnProperty.call(config.frames, String(frame)) ?
        config.frames[frame] : config.default;
    return isSpacing(spacing) ? spacing : null;
}

// CVAT polyline shapes and tracks store their geometry in image coordinates and
// force object rotation to zero. View rotation, pan and zoom are not inputs here.
export function polylineLength(points: readonly number[], spacing: PixelSpacing | null = null): number | null {
    if (!Array.isArray(points) || points.length < 4 || points.length % 2 ||
        !points.every((point) => typeof point === 'number' && Number.isFinite(point)) ||
        (spacing !== null && !isSpacing(spacing))) return null;

    const scaleX = spacing?.x ?? 1;
    const scaleY = spacing?.y ?? 1;
    let length = 0;
    for (let i = 2; i < points.length; i += 2) {
        length += Math.hypot((points[i] - points[i - 2]) * scaleX, (points[i + 1] - points[i - 1]) * scaleY);
    }
    return Number.isFinite(length) ? length : null;
}

export function formatLength(length: number | null, unit: 'mm' | 'px' = 'mm'): string | null {
    if (typeof length !== 'number' || !Number.isFinite(length) || length < 0 ||
        (unit !== 'mm' && unit !== 'px')) return null;

    // Keep small nonzero distances visible without padding ordinary measurements
    // with zeros. Scientific notation remains available for extreme magnitudes.
    const rounded = length > 0 && length < 1 ? Number(length.toPrecision(4)) : Number(length.toFixed(2));
    return `${rounded} ${unit}`;
}
