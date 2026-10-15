// SPDX-License-Identifier: MIT
import React, { useCallback, useMemo } from '@modules/react';
import LabelsMapper, { type FullMapping, type LabelInterface } from 'components/model-runner-modal/labels-mapper';
import 'components/model-runner-modal/styles.scss';
import type { LabelSpec, Mapping, Model } from './engine';

export function toMapperLabels(labels: LabelSpec[]): LabelInterface[] {
    return labels.map((label) => ({
        name: label.name,
        type: label.type as LabelInterface['type'],
        color: label.color,
        attributes: (label.attributes ?? []).map((attribute) => ({
            name: attribute.name,
            input_type: (attribute.input_type ?? 'text') as NonNullable<LabelInterface['attributes']>[number]['input_type'],
            values: [...(attribute.values ?? [])],
        })),
        ...(label.sublabels ? { sublabels: toMapperLabels(label.sublabels) } : {}),
    }));
}

// Only adapt the standard mapper's tuple format to the public lambda API.
// Attribute and skeleton-point choices are explicit, not rebuilt by name.
export function serializeMapping(mapping: FullMapping): Mapping {
    return Object.fromEntries(mapping.map(([model, target, attributes, children]) => [model.name, {
        name: target.name,
        attributes: Object.fromEntries(attributes.flatMap(([source, destination]) => (
            source && destination ? [[source.name, destination.name]] : []
        ))),
        ...(model.type === 'skeleton' ? { sublabels: serializeMapping(children) } : {}),
    }]));
}

interface Props {
    model: Model;
    labels: LabelSpec[];
    disabled: boolean;
    onChange(mapping: Mapping): void;
}

export function MappingEditor({ model, labels, disabled, onChange }: Props): JSX.Element {
    // ObjectMapper resets on a change in these array identities. MLModel.labels
    // returns a fresh array, so memoize by the model instance, not that getter.
    const modelLabels = useMemo(() => toMapperLabels(model.labels), [model]);
    const taskLabels = useMemo(() => toMapperLabels(labels), [labels]);
    const update = useCallback((mapping: FullMapping) => {
        if (!disabled) onChange(serializeMapping(mapping));
    }, [disabled, onChange]);
    return (
        <>
            {/* Keep mapper state mounted, but prevent mouse/keyboard edits while running. */}
            <div hidden={disabled}>
                <LabelsMapper modelLabels={modelLabels} taskLabels={taskLabels} onUpdateMapping={update} />
            </div>
            {disabled && <span>ラベル対応は固定されています。</span>}
        </>
    );
}
