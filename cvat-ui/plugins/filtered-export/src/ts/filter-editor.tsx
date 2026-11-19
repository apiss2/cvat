// SPDX-License-Identifier: MIT
import React, { useEffect, useMemo, useState } from '@modules/react';
import {
    AntdConfig, AntdWidgets, Builder, Query, Utils as QbUtils,
    type Config, type ImmutableTree,
} from '@react-awesome-query-builder/antd';
import Collapse from 'antd/lib/collapse';
import { buildFields, completeTree } from '../../../shared/annotation-filter-config';
import { buildScopeFields } from '../../../shared/resource-filter-config';
import type { LabelSpec } from './api';

interface Props {
    labels: LabelSpec[];
    disabled: boolean;
    onChange(filters: object[], valid: boolean, taskFilter: object, jobFilter: object): void;
}
const emptyTree = (): ImmutableTree => QbUtils.loadTree({ type: 'group', id: QbUtils.uuid() });

export default function FilterEditor({ labels, disabled, onChange }: Props): JSX.Element {
    const [objects, setObjects] = useState<ImmutableTree>(emptyTree);
    const [elements, setElements] = useState<ImmutableTree>(emptyTree);
    const [task, setTask] = useState<ImmutableTree>(emptyTree);
    const [job, setJob] = useState<ImmutableTree>(emptyTree);
    const configs = useMemo(() => [
        buildFields(labels), buildFields(labels, true), buildScopeFields('task'), buildScopeFields('job'),
    ].map((fields): Config => ({
        ...AntdConfig, fields,
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
    const report = useMemo(() => {
        const trees = [objects, elements, task, job];
        const active = (index: number): boolean => index !== 1 || hasElements;
        const validity = trees.map((tree, index) => !active(index) || (completeTree(
            QbUtils.getTree(tree), configs[index].operators,
        ) && QbUtils.isValidTree(tree, configs[index])));
        const rules = trees.map((tree, index) => active(index) ?
            QbUtils.jsonLogicFormat(tree, configs[index]).logic || {} : {});
        const filters = rules.slice(0, 2);
        while (filters.length && !Object.keys(filters[filters.length - 1]).length) filters.pop();
        return { validity, rules, filters };
    }, [objects, elements, task, job, configs, hasElements]);
    useEffect(() => {
        const { validity, rules, filters } = report;
        onChange(filters, validity.every(Boolean), rules[2], rules[3]);
    }, [report, onChange]);
    const render = (tree: ImmutableTree, index: number, change: (value: ImmutableTree) => void): JSX.Element => (
        <Query {...displayedConfigs[index]}
            value={tree} onChange={(next) => { if (!disabled) change(next); }}
            renderBuilder={(props: any) => (
                <div className='query-builder-container'><div className='query-builder'><Builder {...props} /></div></div>
            )} />
    );
    const indicator = (indices: number[]): JSX.Element | null => {
        const invalid = indices.some((index) => !report.validity[index]);
        const active = indices.some((index) => Object.keys(report.rules[index]).length > 0);
        if (!invalid && !active) return null;
        const label = invalid ? '未入力の条件' : 'フィルター設定済み';
        return (
            <span role='img' aria-label={label} title={label} style={{
                display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                backgroundColor: invalid ? '#d4380d' : '#1677ff',
            }} />
        );
    };
    return (
        <div className='cvat-filters-modal'>
            <Collapse accordion size='small' items={[
                { key: 'task', label: 'Task', extra: indicator([2]), children: render(task, 2, setTask) },
                { key: 'job', label: 'Job', extra: indicator([3]), children: render(job, 3, setJob) },
                {
                    key: 'images',
                    label: <span title='条件に合うアノテーションがある画像を選択し、他のアノテーションも出力します。'>画像</span>,
                    extra: indicator([0, 1]),
                    children: <>
                        {hasElements && <strong>Objects</strong>}
                        {render(objects, 0, setObjects)}
                        {hasElements && <><strong>Elements</strong>{render(elements, 1, setElements)}</>}
                    </>,
                },
            ]} />
        </div>
    );
}
