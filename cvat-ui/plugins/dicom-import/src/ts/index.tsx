// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import notification from 'antd/lib/notification';
import { updateRequestProgress } from 'actions/requests-actions';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI } from './api';
import { openDialog } from './dialog';

const builder: ComponentBuilder = ({
    core, store, dispatch, actionCreators,
}) => {
    let dialog: ReturnType<typeof openDialog> | undefined;
    let active = true;
    let generation = 0;
    const identity = (): string => {
        const state = store.getState();
        return state.auth.user ? JSON.stringify([state.auth.user.id, state.organizations.current?.id ?? null]) : '';
    };
    let previous = identity();
    const accepted = (requestID: string, owner: string, started: number): void => {
        const current = (): boolean => active && generation === started && identity() === owner;
        // Keep the standard request listener alive when the upload dialog is closed.
        // Defer setup so an accepted upload cannot become a submission error.
        void Promise.resolve().then(async () => {
            if (!current()) return;
            const request = await core.requests.listen(requestID, {
                callback: (update) => {
                    if (current()) updateRequestProgress(update, dispatch);
                },
            });
            if (!current()) return;
            const key = `dicom-import:${requestID}`;
            if (request.status !== 'finished') {
                notification.info({ key, message: 'DICOMの登録が中止されました。' });
                return;
            }
            const warning = request.message.startsWith('WARNING: DICOM:');
            notification[warning ? 'warning' : 'success']({
                key,
                message: warning ? '一部のDICOMタスクを登録できませんでした。' : 'DICOMの登録が完了しました。',
                description: request.message,
            });
        }).catch((reason) => {
            if (current()) notification.error({
                key: `dicom-import:${requestID}`,
                message: 'DICOM取り込みでエラーが発生しました。',
                description: reason instanceof Error ? reason.message : String(reason),
            });
        });
    };
    const component = (({ targetProps }: { targetProps: {
        projectInstance?: { id: number; name: string; dimension?: string };
    } }) => {
        const project = targetProps.projectInstance;
        return {
            key: 'dicom-import',
            label: 'DICOM import',
            disabled: !project || Boolean(project.dimension && project.dimension !== '2d') ||
                store.getState().projects.selected.length > 1,
            onClick: () => {
                const owner = identity();
                const started = generation;
                if (!project || !owner) return;
                if (dialog) { dialog.focus(); return; }
                const api = createAPI((request) => Axios.request(request), core.config.backendAPI, project.id, store.getState().organizations.current?.slug ?? '', identity);
                dialog = openDialog(api, project.name, () => { dialog = undefined; },
                    (requestID) => accepted(requestID, owner, started));
            },
        };
    }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    dispatch(actionCreators.addUIComponent('projectActions.items', component, { weight: 15 }));
    return {
        name: 'DICOM project import',
        globalStateDidUpdate: (): void => {
            const next = identity();
            if (next !== previous) { generation++; previous = next; dialog?.dispose(); }
        },
        destructor: (): void => {
            active = false;
            dialog?.dispose();
            dispatch(actionCreators.removeUIComponent('projectActions.items', component));
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
