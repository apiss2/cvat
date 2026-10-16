// SPDX-License-Identifier: MIT
import type { Config, Field } from '@react-awesome-query-builder/antd';
import type { LabelSpec } from './api';

// The display filter's field names and JsonLogic representation are retained.
// Browser-local ObjectID and transient Votes are not persisted export criteria.
const adjusted = (name: string): string => name.replace(/\./g, '\u2219');
const numeric = ['equal', 'not_equal', 'less', 'less_or_equal', 'greater', 'greater_or_equal',
    'between', 'not_between', 'is_null', 'is_not_null'];
const text = ['equal', 'not_equal', 'like', 'not_like', 'is_empty', 'is_not_empty'];
const select = ['select_equals', 'select_not_equals', 'select_any_in', 'select_not_any_in'];
const choice = (label: string, values: string[]): Field => ({
    label, type: 'select', operators: select, valueSources: ['value'],
    fieldSettings: { listValues: values.map((value) => ({ value, title: value })) },
});
const quantity = (label: string): Field => ({ label, type: 'number', operators: numeric });
const boolean = (label: string): Field => ({ label, type: 'boolean', operators: ['equal', 'not_equal'] });

export function buildFields(labels: LabelSpec[], elements = false): Config['fields'] {
    const specs = elements ? labels.flatMap((label) => label.sublabels.map((child) => ({
        ...child, name: `${label.name} / ${child.name}`,
    }))) : labels;
    const attributes: Config['fields'] = Object.fromEntries(specs.filter((label) => label.attributes.length).map((label) => [
        adjusted(label.name), {
            type: '!struct', label: label.name,
            subfields: Object.fromEntries(label.attributes.map((attribute) => {
                const type = attribute.input_type;
                const field = type === 'number' ? quantity(attribute.name) :
                    type === 'checkbox' ? boolean(attribute.name) :
                        ['radio', 'select'].includes(type) ? choice(attribute.name, attribute.values) :
                            { label: attribute.name, type: 'text', operators: text };
                return [adjusted(attribute.name), field];
            })),
        },
    ]));
    return {
        label: choice('Label', specs.map((label) => label.name)),
        occluded: boolean('Occluded'),
        ...(!elements ? {
            type: choice('Type', ['shape', 'track', 'tag']),
            shape: choice('Shape', ['rectangle', 'points', 'polyline', 'polygon', 'cuboid', 'ellipse', 'skeleton', 'mask']),
            source: choice('Source', ['auto', 'semi-auto', 'manual', 'file', 'consensus']),
            width: quantity('Width'), height: quantity('Height'), rotation: quantity('Rotation'),
            serverID: { ...quantity('ServerID'), hideForCompare: true },
            score: quantity('Score'), zOrder: quantity('Z order'),
        } : {}),
        ...(Object.keys(attributes).length ? {
            attr: { label: 'Attributes', type: '!struct', subfields: attributes },
        } : {}),
    };
}

interface Tree {
    type: string;
    children1?: Tree[] | Record<string, Tree>;
    properties?: { field?: string; operator?: string; value?: unknown[] };
}

// The builder may omit an unfinished rule when formatting JsonLogic. Reject it
// rather than widening a mixed or empty condition into an accidental full export.
export function completeTree(input: unknown, operators: Record<string, { cardinality?: number }>, root = true): boolean {
    if (!input || typeof input !== 'object') return false;
    const tree = input as Tree;
    if (tree.type === 'group') {
        const children = Object.values(tree.children1 || {});
        return children.length ? children.every((child) => completeTree(child, operators, false)) : root;
    }
    if (tree.type !== 'rule') return false;
    const { field, operator, value = [] } = tree.properties || {};
    if (!field || !operator || !operators[operator]) return false;
    const count = operators[operator].cardinality ?? 1;
    return value.length >= count && value.slice(0, count).every((entry) => entry !== null &&
        entry !== undefined && (!Array.isArray(entry) || entry.length > 0));
}
