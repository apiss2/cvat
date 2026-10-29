// SPDX-License-Identifier: MIT
import Axios from '@modules/axios';
import notification from 'antd/lib/notification';
import { updateRequestProgress } from 'actions/requests-actions';
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createAPI } from './api';
import { openDialog } from './dialog';
import { errorMessage, readSummary, summaryMessage } from './messages';

const builder: ComponentBuilder = ({ core, store, dispatch, actionCreators }) => {
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
        void Promise.resolve().then(async () => {
            if (!current()) return;
            const request = await core.requests.listen(requestID, {
                callback: (update) => {
                    if (current()) updateRequestProgress(update, dispatch);
                },
            });
            if (!current()) return;
            const key = `project-annotations:${requestID}`;
            if (request.status !== 'finished') {
                notification.info({ key, message: 'アノテーションの取り込みが中止されました。' });
                return;
            }
            const summary = readSummary(request.message);
            const warning = summary && (summary.failed > 0 || summary.unmatched > 0 || summary.invalid > 0);
            notification[warning ? 'warning' : 'success']({
                key,
                message: warning ? '取り込めなかったTaskまたはレコードがあります。' : 'アノテーションの取り込みが完了しました。',
                description: summary ? summaryMessage(summary) : 'Requestsで結果を確認してください。',
            });
        }).catch((reason) => {
            if (current()) notification.error({
                key: `project-annotations:${requestID}`,
                message: 'アノテーションの取り込みでエラーが発生しました。',
                description: errorMessage(reason),
            });
        });
    };
    const component = (({ targetProps }: { targetProps: {
        projectInstance?: { id: number; name: string; dimension?: string };
    } }) => {
        const project = targetProps.projectInstance;
        return {
            key: 'project-annotations', label: 'Import annotations',
            disabled: !project || store.getState().projects.selected.length > 1,
            onClick: () => {
                const owner = identity();
                const started = generation;
                if (!project || !owner) return;
                if (dialog) { dialog.focus(); return; }
                const api = createAPI((request) => Axios.request(request), core.config.backendAPI,
                    project.id, store.getState().organizations.current?.slug ?? '', identity, project.dimension);
                dialog = openDialog(api, project.name, () => { dialog = undefined; },
                    (requestID) => accepted(requestID, owner, started));
            },
        };
    }) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    dispatch(actionCreators.addUIComponent('projectActions.items', component, { weight: 14 }));
    return {
        name: 'Project annotation import',
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
