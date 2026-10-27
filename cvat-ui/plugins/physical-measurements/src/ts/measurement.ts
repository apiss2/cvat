// SPDX-License-Identifier: MIT

export interface PixelSpacing {
    x: number;
    y: number;
    unit: 'mm';
}

export interface DicomTagState {
    objectType: string;
    frame: number;
    label: { name: string; attributes: readonly { id?: number; name: string }[] };
    attributes: Readonly<Record<number, unknown>>;
}

function isSpacing(value: PixelSpacing): boolean {
    return typeof value.x === 'number' && Number.isFinite(value.x) && value.x > 0 &&
        typeof value.y === 'number' && Number.isFinite(value.y) && value.y > 0 && value.unit === 'mm';
}

// DICOM DS permits decimal/scientific notation, but not hexadecimal, Infinity
// or empty components. PixelSpacing stores row (Y), then column (X), in mm.
export function parsePixelSpacing(value: unknown): PixelSpacing | null {
    if (typeof value !== 'string') return null;
    const parts = value.split('\\').map((part) => part.trim());
    // eslint-disable-next-line security/detect-unsafe-regex -- Decimal/exponent separators bound backtracking.
    const decimal = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$/;
    if (parts.length !== 2 || !parts.every((part) => decimal.test(part))) return null;
    const spacing: PixelSpacing = { x: Number(parts[1]), y: Number(parts[0]), unit: 'mm' };
    return isSpacing(spacing) ? spacing : null;
}

export function resolveSpacing(states: readonly DicomTagState[], frame: number): PixelSpacing | null {
    if (!Number.isSafeInteger(frame) || frame < 0) return null;
    let spacing: PixelSpacing | null = null;
    for (const state of states) {
        if (state.objectType !== 'tag' || state.frame !== frame || state.label.name !== 'DicomTag') continue;
        const specs = state.label.attributes.filter((attribute) => attribute.name === 'PixelSpacing');
        if (specs.length !== 1 || typeof specs[0].id !== 'number') return null;
        const { id } = specs[0];
        if (!Object.prototype.hasOwnProperty.call(state.attributes, id)) return null;
        const current = parsePixelSpacing(state.attributes[id]);
        // Conflicting or malformed duplicate tags cannot establish a calibration.
        if (!current || (spacing && (current.x !== spacing.x || current.y !== spacing.y))) return null;
        spacing = current;
    }
    return spacing;
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
