// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import notification from '@modules/antd/lib/notification';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import {
    createAPI, createEditorAPI, errorMessage, MeasurementAPI,
} from './api';
import { createDisplay, MeasurementCanvas } from './display';
import { openDialog, DialogHandle } from './dialog';

type Task = { id: number; name: string; dimension: string };
type Job = { id: number; taskId: number; dimension: string };

const builder: ComponentBuilder = ({
    core, store, dispatch, actionCreators,
}) => {
    let dialog: DialogHandle | undefined;
    let disposed = false;
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([
            state.auth.user.id, state.organizations.current?.id ?? null, state.organizations.current?.slug ?? '',
        ]) : '';
    };
    const api = (type: 'task' | 'job', id: number): MeasurementAPI => createAPI(
        (request) => Axios.request(request), core.config.backendAPI, type, id, store.getState().organizations.current?.slug ?? '', identity,
    );
    const display = createDisplay((error) => {
        notification.warning({
            key: 'physical-measurements-load',
            message: '物理長の設定を読み込めませんでした',
            description: errorMessage(error),
        });
    });
    let lastIdentity = identity();
    let activeKey = '';
    let activeCanvas: MeasurementCanvas | null = null;
    const sync = (): void => {
        if (disposed) return;
        const owner = identity();
        if (owner !== lastIdentity) {
            lastIdentity = owner;
            dialog?.dispose();
        }
        const state = store.getState();
        const { job, canvas } = state.annotation;
        const currentCanvas = canvas.instance as MeasurementCanvas | null;
        const key = owner && job.instance?.dimension === '2d' && !job.fetching && currentCanvas ?
            `${owner}:${job.instance.id}` : '';
        if (key === activeKey && currentCanvas === activeCanvas) return;
        // Wait for the first setup of a new job. A frame change keeps its existing formatter.
        if (!key || !canvas.ready) {
            activeKey = '';
            activeCanvas = null;
            display.update(null);
            return;
        }
        activeKey = key;
        activeCanvas = currentCanvas;
        display.update({ key, canvas: currentCanvas, api: api('job', job.instance!.id) });
    };
    const refresh = (): void => { if (!disposed && !document.hidden) display.refresh(); };
    window.addEventListener('focus', refresh);

    const open = (taskID: number, title: string, frame: number, jobID?: number): void => {
        if (!identity()) return;
        if (dialog) { dialog.focus(); return; }
        const editor = createEditorAPI(api('task', taskID), jobID ? api('job', jobID) : undefined);
        dialog = openDialog(editor, { title, frame, onSaved: () => { display.refresh(); } }, () => {
            dialog = undefined;
        });
    };
    const taskMenu = (({ targetProps }: { targetProps: { taskInstance?: Task } }) => {
        const task = targetProps.taskInstance;
        return {
            key: 'physical-measurements',
            label: '物理長',
            disabled: !task || task.dimension !== '2d' || store.getState().tasks.selected.length > 1,
            onClick: () => { if (task) open(task.id, `Task #${task.id} ${task.name}`, 0); },
        };
    }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    const jobMenu = (({ targetProps }: { targetProps: { jobInstance?: Job } }) => {
        const job = targetProps.jobInstance;
        return {
            key: 'physical-measurements',
            label: '物理長',
            disabled: !job || job.dimension !== '2d',
            onClick: () => {
                if (job) {
                    open(job.taskId, `Task #${job.taskId}`, store.getState().annotation.player.frame.number, job.id);
                }
            },
        };
    }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    const entries = [
        { path: 'taskActions.items', component: taskMenu },
        { path: 'annotationPage.menuActions.items', component: jobMenu },
    ];
    entries.forEach(({ path, component }) => dispatch(actionCreators.addUIComponent(path, component, { weight: 45 })));
    sync();
    return {
        name: 'Physical polyline measurements',
        globalStateDidUpdate: sync,
        destructor: (): void => {
            disposed = true;
            window.removeEventListener('focus', refresh);
            dialog?.dispose();
            display.dispose();
            entries.forEach(({ path, component }) => dispatch(actionCreators.removeUIComponent(path, component)));
        },
    };
};

let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) { registered = true; host.registerComponent(builder); }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
