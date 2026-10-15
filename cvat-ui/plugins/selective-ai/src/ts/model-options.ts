// SPDX-License-Identifier: MIT

export function supportsThreshold(model: { id: string | number }): boolean {
    // registry/gateway.py reserves mr- and routes these IDs to the registry.
    // Its prediction contract does not use the CVAT threshold. Do not infer
    // capabilities from a display name, label type, or the ONNX file format.
    return !String(model.id).startsWith('mr-');
}

export function thresholdParameters(
    model: { id: string | number }, value: number | null,
): { threshold?: number } {
    if (!supportsThreshold(model)) return {};
    if (value === null || !Number.isFinite(value) || value < 0 || value > 1) {
        throw new Error('0から1の閾値を指定してください。');
    }
    return { threshold: value };
}
