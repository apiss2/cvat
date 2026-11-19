// SPDX-License-Identifier: MIT
import type { Config } from '@react-awesome-query-builder/antd';
import { config as tasksConfig } from 'components/tasks-page/tasks-filter-configuration';
import { config as jobsConfig } from 'components/jobs-page/jobs-filter-configuration';

// Identical fields, operators, widgets and async user search to the list pages.
// The consumers still restrict the eligible resources to their operation scope.
export function buildScopeFields(scope: 'task' | 'job'): Config['fields'] {
    return (scope === 'task' ? tasksConfig : jobsConfig).fields!;
}
