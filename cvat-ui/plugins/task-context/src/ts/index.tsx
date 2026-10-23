// SPDX-License-Identifier: MIT

import React, {
    useCallback, useEffect, useState, useSyncExternalStore,
} from '@modules/react';
import Axios from '@modules/axios';
import { InfoCircleOutlined } from '@ant-design/icons';
import Button from 'antd/lib/button';
import Modal from 'antd/lib/modal';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI, type API } from './api';
import TaskContextPanel from './panel';

type TaskProps = { targetProps: { task: { id: number } } };
type InfoProps = { targetProps: { jobInstance: { id: number; taskId: number }; visible: boolean } };

const builder: ComponentBuilder = ({
    core, store, dispatch, actionCreators,
}) => {
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([state.auth.user.id, state.organizations.current?.id ?? null]) : '';
    };
    const useIdentity = (): string => useSyncExternalStore(store.subscribe, identity, identity);

    function TaskButton({ targetProps }: TaskProps): JSX.Element | null {
        const owner = useIdentity();
        const [openedFor, setOpenedFor] = useState('');
        const taskId = targetProps.task.id;
        const key = `${owner}:${taskId}`;
        useEffect(() => setOpenedFor(''), [owner, taskId]);
        const apiFactory = useCallback((): API => createAPI(
            (request) => Axios.request(request),
            core.config.backendAPI,
            { type: 'tasks', id: taskId, taskId },
            store.getState().organizations.current?.slug ?? '',
            identity,
        ), [owner, taskId]);
        if (!owner) return null;
        const open = openedFor === key;
        return (
            <>
                <Button icon={<InfoCircleOutlined />} onClick={() => setOpenedFor(key)}>タスク情報</Button>
                <Modal open={open} width={800} footer={null} destroyOnClose
                    title='タスク情報' onCancel={() => setOpenedFor('')}>
                    {open && <TaskContextPanel key={key} taskId={taskId} createAPI={apiFactory} />}
                </Modal>
            </>
        );
    }

    function InfoSection({ targetProps }: InfoProps): JSX.Element | null {
        const owner = useIdentity();
        const { jobInstance, visible } = targetProps;
        const { id, taskId } = jobInstance;
        const apiFactory = useCallback((): API => createAPI(
            (request) => Axios.request(request),
            core.config.backendAPI,
            { type: 'jobs', id, taskId },
            store.getState().organizations.current?.slug ?? '',
            identity,
        ), [owner, id, taskId]);
        if (!owner || !visible) return null;
        return <TaskContextPanel key={`${owner}:${id}`} taskId={taskId} createAPI={apiFactory} />;
    }

    const entries = [
        { path: 'taskPage.details.topBar.extras', component: TaskButton },
        { path: 'annotationPage.info.sections', component: InfoSection },
    ];
    entries.forEach(({ path, component }) => dispatch(actionCreators.addUIComponent(path, component, { weight: 30 })));
    return {
        name: 'Task context',
        destructor: (): void => {
            entries.forEach(({ path, component }) => dispatch(actionCreators.removeUIComponent(path, component)));
        },
    };
};
let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) {
        registered = true;
        host.registerComponent(builder);
    }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
