// SPDX-License-Identifier: MIT
import React, { useEffect, useMemo, useState } from '@modules/react';
import {
    AntdConfig, AntdWidgets, Builder, Query, Utils as QbUtils,
    type Config, type ImmutableTree,
} from '@react-awesome-query-builder/antd';
import { buildFields, completeTree } from './filter-config';
import type { LabelSpec } from './api';

interface Props {
    labels: LabelSpec[];
    disabled: boolean;
    onChange(filters: object[], valid: boolean): void;
}
const emptyTree = (): ImmutableTree => QbUtils.loadTree({ type: 'group', id: QbUtils.uuid() });

export default function FilterEditor({ labels, disabled, onChange }: Props): JSX.Element {
    const [objects, setObjects] = useState<ImmutableTree>(emptyTree);
    const [elements, setElements] = useState<ImmutableTree>(emptyTree);
    const configs = useMemo(() => [false, true].map((keypoints): Config => ({
        ...AntdConfig, fields: buildFields(labels, keypoints),
        settings: { ...AntdConfig.settings,
            renderField: (props: any) => <AntdWidgets.FieldDropdown {...props} />,
        },
    })), [labels]);
    const displayedConfigs = useMemo(() => configs.map((config) => ({
        ...config, settings: { ...config.settings,
            immutableGroupsMode: disabled, immutableFieldsMode: disabled,
            immutableOpsMode: disabled, immutableValuesMode: disabled,
            canReorder: !disabled, canRegroup: !disabled,
        },
    })), [configs, disabled]);
    const hasElements = labels.some((label) => label.sublabels.length > 0);
    useEffect(() => {
        const trees = hasElements ? [objects, elements] : [objects];
        const valid = trees.every((tree, index) => completeTree(
            QbUtils.getTree(tree), configs[index].operators,
        ) && QbUtils.isValidTree(tree, configs[index]));
        const filters = trees.map((tree, index) => QbUtils.jsonLogicFormat(tree, configs[index]).logic || {});
        while (filters.length && !Object.keys(filters[filters.length - 1]).length) filters.pop();
        onChange(filters, valid);
    }, [objects, elements, configs, hasElements, onChange]);
    const render = (tree: ImmutableTree, index: number, change: (value: ImmutableTree) => void): JSX.Element => (
        <Query {...displayedConfigs[index]}
            value={tree} onChange={(next) => { if (!disabled) change(next); }}
            renderBuilder={(props: any) => (
                <div className='query-builder-container'><div className='query-builder'><Builder {...props} /></div></div>
            )} />
    );
    return (
        <div className='cvat-filters-modal'>
            <strong>Objects</strong>
            {render(objects, 0, setObjects)}
            {hasElements && <><strong>Elements</strong>{render(elements, 1, setElements)}</>}
        </div>
    );
}
